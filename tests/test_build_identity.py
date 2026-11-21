# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which exact build is running.

Every build used to answer ``0.2.0``: from ``--version`` and from the MCP
handshake (an empty ``serverInfo.version``). These tests pin where each
surface gets the commit from, and that an unreadable identity says
``unknown`` instead of guessing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.client import Client

from modelquad import buildinfo, executable_resolution
from modelquad.branding import installed_version

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
BUILT = "2026-09-27T10:00:00+00:00"
VERSION = installed_version("unknown")


@pytest.fixture(autouse=True)
def _fresh_identity():
    buildinfo.current_build_info.cache_clear()
    yield
    buildinfo.current_build_info.cache_clear()


# ---------------------------------------------------------------- resolution


def test_a_source_checkout_asks_git_for_head(tmp_path):
    seen: list[Path] = []

    def head(root: Path) -> str:
        seen.append(root)
        return SHA + "\n"

    info = buildinfo.read_build_info(source_root=tmp_path, git_head=head)
    assert seen == [tmp_path]
    assert info.channel == "source" and info.source_commit == SHA and info.built_at_utc == "unknown"
    assert info.label == f"{VERSION}+{SHA[:7]} (source)"


@pytest.mark.parametrize(
    "head", [None, "not a sha", "abc123", SHA.upper() + "0"],
    ids=["no-answer", "not-a-sha", "short-sha", "too-long"],
)
def test_a_head_that_is_not_a_full_sha_is_unknown_never_guessed(tmp_path, head):
    info = buildinfo.read_build_info(source_root=tmp_path, git_head=lambda root: head)
    assert info.to_dict() == {
        "version": VERSION,
        "source_commit": "unknown",
        "short": "unknown",
        "built_at_utc": "unknown",
        "channel": "source",
    }
    assert info.identity == VERSION
    assert info.label == f"{VERSION} (unknown build)"


def test_a_source_checkout_git_cannot_answer_for_is_unknown(tmp_path):
    info = buildinfo.read_build_info(source_root=tmp_path, git_head=lambda root: None)
    assert info.channel == "source" and info.label == f"{VERSION} (unknown build)"


def test_git_is_asked_read_only(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    root = tmp_path / 'repository'
    root.mkdir()
    executable = tmp_path / 'synthetic install' / 'git.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'synthetic fixture - never execute\n')
    resolved = str(executable.resolve())

    def resolve(name, *, exclude_roots):
        assert name == 'git' and exclude_roots == (root,)
        return executable_resolution.Resolution(resolved, 'path', 'binary', None)

    monkeypatch.setattr(buildinfo, 'resolve_executable', resolve)

    class Done:
        returncode = 0
        stdout = SHA + "\n"

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return Done()

    monkeypatch.setattr(buildinfo.subprocess, "run", fake_run)
    assert buildinfo._git_head(root) == SHA
    (argv, kwargs), = calls
    assert argv == [resolved, "-C", str(root), "rev-parse", "--verify", "HEAD"]
    assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert kwargs["timeout"] > 0


def test_this_checkout_resolves_to_its_own_head():
    info = buildinfo.read_build_info()
    assert info.channel == "source"
    assert info.source_commit == "unknown" or len(info.source_commit) == 40


# ---------------------------------------------------------------- surfaces


def test_the_build_names_no_path(tmp_path):
    info = buildinfo.read_build_info(source_root=tmp_path, git_head=lambda root: SHA)
    assert info.to_dict() == {
        "version": VERSION,
        "source_commit": SHA,
        "short": SHA[:7],
        "built_at_utc": "unknown",
        "channel": "source",
    }
    for value in (*info.to_dict().values(), info.identity, info.label):
        assert "/" not in value and "\\" not in value
        assert str(tmp_path) not in value


def test_version_flag_prints_the_same_build(tmp_path, monkeypatch, capsys):
    from modelquad import cli
    from modelquad.branding import PRODUCT_NAME

    expected = buildinfo.read_build_info(source_root=tmp_path, git_head=lambda root: SHA)
    monkeypatch.setattr(buildinfo, "read_build_info", lambda: expected)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"{PRODUCT_NAME} {VERSION}+{SHA[:7]} (source)"


def test_the_mcp_server_is_built_with_the_build_identity(tmp_path, monkeypatch):
    from modelquad.mcp import server as mcp_server

    expected = buildinfo.read_build_info(source_root=tmp_path, git_head=lambda root: SHA)

    def broken():
        raise OSError("unreadable")

    # A context of its own: this module's fixture clears the real cache at teardown.
    with monkeypatch.context() as patch:
        patch.setattr(buildinfo, "current_build_info", lambda: expected)
        assert mcp_server._build_identity() == expected.identity == f"{VERSION}+{SHA[:7]}"
        # An unreadable identity leaves the version out; it never guesses one.
        patch.setattr(buildinfo, "current_build_info", broken)
        assert mcp_server._build_identity() is None


def test_mcp_initialize_names_the_build():
    from modelquad.mcp import server as mcp_server

    async def handshake():
        async with Client(mcp_server.server, mode="legacy") as client:
            return client.server_info

    server_info = asyncio.run(handshake())
    assert server_info is not None
    assert server_info.version == buildinfo.current_build_info().identity
    assert server_info.version.startswith(VERSION)


def test_a_copy_pip_took_from_a_package_index_names_the_release(monkeypatch):
    """No checkout to ask: pip's own record (no direct URL) says it came from an index."""
    monkeypatch.setattr(buildinfo, "_source_checkout_root", lambda: None)
    info = buildinfo.read_build_info(install_kind=lambda: "pypi")
    assert info.channel == buildinfo.CHANNEL_INDEX
    assert info.label == f"{VERSION} (release from a package index)"
    assert info.source_commit == "unknown"  # the commit itself is still not claimed


@pytest.mark.parametrize("kind", ["clone", "editable", "unknown"])
def test_any_other_installed_copy_stays_an_unknown_build(kind, monkeypatch):
    monkeypatch.setattr(buildinfo, "_source_checkout_root", lambda: None)
    info = buildinfo.read_build_info(install_kind=lambda: kind)
    assert info.label == f"{VERSION} (unknown build)"


def test_an_unreadable_install_record_stays_unknown(monkeypatch):
    monkeypatch.setattr(buildinfo, "_source_checkout_root", lambda: None)

    def broken():
        raise OSError("no record")

    assert buildinfo.read_build_info(install_kind=broken).label == f"{VERSION} (unknown build)"
