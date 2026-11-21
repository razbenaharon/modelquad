# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A folder that is not local is refused before anything opens it.

Statting ``\\\\host\\share`` makes Windows connect to that host and send the
user's NTLM response, outside the loopback guard's sight. The entry points
tested here (the status, package and review tools, the review flow's own
gate, ``settings repo``, ``settings show``, ``review package`` and ``doctor``)
refuse such a path before any file-system call, and a ``.git`` pointer to a
share is never followed.
"""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import anyio
import pytest

from modelquad import localpath, repo_policy as rp, repo_settings as rs, review
from modelquad.cli import main
from modelquad.mcp import server as mcp
from modelquad.procutil import no_window_kwargs

REMOTE = [r"\\attacker.example\share", r"\\attacker.example@80\x", "//attacker.example/share",
          r"\\?\UNC\attacker.example\share", r"\\.\pipe\x", r"\\?\C:\Users"]


@pytest.mark.parametrize("path", REMOTE)
def test_network_and_device_paths_are_refused_by_text(path):
    assert localpath.text_reason(path) == "network_or_device_path"
    assert localpath.reason(path) is not None and not localpath.is_local(path)


@pytest.mark.parametrize("path,why", [
    ("relative", "not_absolute"), ("C:relative", "not_absolute"), ("", "invalid_path"),
    (r"C:\work\NUL", "device_path"), (r"C:\work\con.txt", "device_path"),
    (r"C:\work\file:stream", "invalid_path"), ("C:\\work\x00", "invalid_path"), (None, "not_a_path"),
])
def test_relative_device_and_stream_paths_are_refused(path, why):
    assert localpath.text_reason(path) == why


def test_a_local_folder_passes(tmp_path):
    assert localpath.reason(str(tmp_path)) is None
    assert localpath.reason(tmp_path) is None


def test_a_link_to_a_share_is_refused_without_following_it(tmp_path, monkeypatch):
    link = tmp_path / "linked"
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if os.path.normcase(str(path)) == os.path.normcase(str(link)):
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        if str(path).startswith("\\\\"):
            pytest.fail("a share was opened")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(localpath.os, "lstat", lstat)
    monkeypatch.setattr(localpath.os, "readlink", lambda path: r"\\attacker.example\share")
    assert localpath.reason(str(link / "project")) == "links_to_network"


def test_a_local_junction_and_a_cloud_folder_pass(tmp_path, monkeypatch):
    import subprocess

    target = tmp_path / "real"
    target.mkdir()
    junction = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                   check=True, capture_output=True, **no_window_kwargs())
    assert localpath.reason(str(junction / "project")) is None
    cloud = tmp_path / "OneDrive"
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if os.path.normcase(str(path)) == os.path.normcase(str(cloud)):
            # IO_REPARSE_TAG_CLOUD_6: a placeholder folder, not a link.
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=0x9000601A)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(localpath.os, "lstat", lstat)
    monkeypatch.setattr(localpath.os, "readlink", lambda path: pytest.fail("not a link"))
    assert localpath.reason(str(cloud / "project")) is None


@pytest.mark.parametrize("pointer",[r"\\host\share\repo.git", "//host/share/x", r"\\?\UNC\host\s"])
def test_remote_pointers_are_recognised(pointer):
    assert localpath.remote_pointer(pointer)
    assert not localpath.remote_pointer(r"\\?\C:\repo\.git")
    assert not localpath.remote_pointer(r"C:\repo\.git")


# --------------------------------------------------------------- entry points


@pytest.fixture
def share_tripwire(monkeypatch):
    """Fail the test the moment anything stats, resolves or lists a share."""
    touched = []

    def guard(function):
        def wrapped(path, *args, **kwargs):
            text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
            if isinstance(text, str) and text.replace("/", "\\").startswith("\\\\"):
                touched.append(text)
                raise AssertionError("a network path was opened")
            return function(path, *args, **kwargs)
        return wrapped

    for name in ("stat", "lstat", "listdir", "scandir"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))
    monkeypatch.setattr(os.path, "realpath", guard(os.path.realpath))
    monkeypatch.setattr(os.path, "isdir", guard(os.path.isdir))
    return touched


@pytest.mark.parametrize("path", REMOTE[:4])
def test_status_does_not_open_a_share(share_tripwire, path):
    result = mcp.status(project_directory=path)
    # Nothing about the folder is read or reported: not its mode, not its rules.
    assert "folder_rules" not in result.structured_content
    assert "own_grant" not in result.structured_content
    assert share_tripwire == []


class _Started:
    """Stands in for Codex: notes each request that passed the gates."""

    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        raise AssertionError("a share reached the executor")


@pytest.mark.parametrize("path", REMOTE)
def test_delegate_refuses_a_share_before_opening_it(share_tripwire, monkeypatch, path):
    executor = _Started()
    monkeypatch.setattr(mcp, "_executor", lambda: executor)

    async def invoke():
        return await mcp.delegate(task_type="review", prompt="Review it.",
                                            working_directory=path, files=["a.py"], timeout=60.0)

    result = anyio.run(invoke)
    payload = result.structured_content
    assert payload["reason"] == localpath.UNUSABLE
    assert payload["refused"] is True and payload["started"] is False
    assert result.content[0].text.splitlines()[0] == "Codex was not started."
    assert executor.requests == [] and share_tripwire == []


@pytest.mark.parametrize("path", REMOTE)
def test_the_package_tool_refuses_a_share_before_opening_it(share_tripwire, path):
    async def invoke():
        return await mcp.review_package(task_type="review", prompt="Review it.",
                                                  working_directory=path, files=["a.py"])

    payload = anyio.run(invoke).structured_content
    assert payload["reason"] == localpath.UNUSABLE and payload["refused"] is True
    assert "files" not in payload and share_tripwire == []


@pytest.mark.parametrize("path", REMOTE)
@pytest.mark.parametrize("starts_codex", [True, False])
@pytest.mark.parametrize("standing", [True, False])
def test_the_review_gate_refuses_a_share_before_any_other_rule_reads_it(
        share_tripwire, path, starts_codex, standing):
    request = review.ReviewRequest(task_type="review", prompt="Review it.", working_directory=path,
                                   files=("a.py",))
    session = review.Session(standing=standing, project=path)
    refused = review.gate(request, session, starts_codex=starts_codex)
    assert refused["reason"] == localpath.UNUSABLE and refused["message"] == rp.UNUSABLE_MESSAGE
    assert share_tripwire == []


def test_the_gate_and_the_repository_readers_never_open_a_share(share_tripwire):
    refusal, policy = rp.gate(REMOTE[0], "review")
    assert refusal["reason"] == localpath.UNUSABLE and policy is None
    assert rp.load(REMOTE[0]).refusal == rp.UNREADABLE
    assert rs.local_policy(REMOTE[0]).refusal == rp.UNREADABLE
    assert rs.repo_key(REMOTE[0], "0" * 32) is None
    assert share_tripwire == []


@pytest.mark.parametrize("argv", [["settings", "repo", "off", "--project", REMOTE[0]],
                                  ["settings", "repo", "--project", REMOTE[0]],
                                  ["settings", "show", "--project", REMOTE[0]]])
def test_the_cli_refuses_a_share(share_tripwire, capsys, argv):
    assert main(argv) == 2
    assert "not a local folder" in capsys.readouterr().err
    assert share_tripwire == []


def test_the_cli_package_refuses_a_share(share_tripwire, capsys):
    assert main(["review", "package", "a.py", "--project", REMOTE[0], "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == localpath.UNUSABLE and payload["refused"] is True
    assert "files" not in payload
    assert share_tripwire == []


def test_doctor_reports_a_share_without_opening_it(share_tripwire):
    from modelquad import setupcheck

    check = setupcheck._folder(REMOTE[0])
    assert check["ok"] is False and check["detail"] == rp.UNUSABLE_MESSAGE
    assert share_tripwire == []


def test_a_gitdir_pointer_to_a_share_is_not_followed(tmp_path, share_tripwire):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: " + REMOTE[0] + "\\repo.git\n", encoding="utf-8")
    with pytest.raises(OSError):
        rs._git_common_dir(worktree)
    assert rs.repo_key(worktree, "0" * 32) is None
    assert share_tripwire == []


def test_a_commondir_pointer_to_a_share_is_not_followed(tmp_path, share_tripwire):
    git_dir = tmp_path / "main.git" / "worktrees" / "w"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text(REMOTE[0] + "\n", encoding="utf-8")
    worktree = tmp_path / "w"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    with pytest.raises(OSError):
        rs._git_common_dir(worktree)
    assert share_tripwire == []


def test_git_is_not_started_in_a_folder_whose_pointer_leads_to_a_share(tmp_path, monkeypatch, share_tripwire):
    from modelquad import gitmeta as changes

    monkeypatch.setattr(changes, "_git", lambda *a, **k: pytest.fail("Git would follow the pointer"))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: //attacker.example/share/repo.git\n", encoding="utf-8")
    assert rs.git_pointer_leaves_machine(worktree) is True
    assert rp.load(worktree).refusal == rp.UNREADABLE
    assert rp.gate(str(worktree), "review")[0]["reason"] == rp.UNREADABLE
    assert share_tripwire == []


def test_an_ordinary_repository_and_a_submodule_pointer_stay_local(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git" / "modules" / "lib").mkdir(parents=True)
    assert rs.git_pointer_leaves_machine(repo) is False
    sub = repo / "lib"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/lib\n", encoding="utf-8")
    assert rs.git_pointer_leaves_machine(sub) is False
