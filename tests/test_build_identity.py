# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which exact build is running.

Every build used to answer ``0.2.0``: from ``--version``, from
``/api/health`` and from the MCP handshake (an empty ``serverInfo.version``).
These tests pin where each surface gets the commit from, and that an
unreadable identity says ``unknown`` instead of guessing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from orchestrator import buildinfo, executable_resolution
from orchestrator.branding import installed_version
from orchestrator.service.app import create_app

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


def test_health_carries_the_build_and_no_path(tmp_path, monkeypatch):
    expected = buildinfo.read_build_info(source_root=tmp_path, git_head=lambda root: SHA)
    monkeypatch.setattr(buildinfo, "read_build_info", lambda: expected)

    app = create_app(db_path=tmp_path / "service.db", poll_interval=3600.0, poll_providers=False,
                     write_secret="build-identity-secret")
    payload = TestClient(app, base_url="http://127.0.0.1").get(
        "/api/health", headers={"X-ModelQuad-Token": "build-identity-secret"}).json()

    assert payload["build"] == {
        "version": VERSION,
        "source_commit": SHA,
        "short": SHA[:7],
        "built_at_utc": "unknown",
        "channel": "source",
    }
    for value in payload["build"].values():
        assert "/" not in value and "\\" not in value
    assert str(tmp_path) not in json.dumps(payload["build"])


def test_mcp_initialize_names_the_build():
    from orchestrator.mcp import server as mcp_server

    async def handshake():
        async with Client(mcp_server.server, mode="legacy") as client:
            return client.server_info

    server_info = asyncio.run(handshake())
    assert server_info is not None
    assert server_info.version == buildinfo.current_build_info().identity
    assert server_info.version.startswith(VERSION)
