# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The data-flow notice reaches a user before Codex receives anything.

``modelquad onboard`` and ``modelquad install`` print it. The plugin can put
ModelQuad in front of someone who ran neither, so their first call that would
start Codex returns the notice instead, once, and the background quota probe
the notice describes waits for it too.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from modelquad import cli, disclosure, paths
from modelquad.mcp import server

# Captured at collection, before conftest treats the notice as shown.
REAL_NOTICE_SHOWN = disclosure.notice_shown


@pytest.fixture
def fresh_install(monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(root))
    monkeypatch.setattr(disclosure, "notice_shown", REAL_NOTICE_SHOWN)
    return root


def _payload(result) -> dict:
    return result.structured_content


def test_the_first_codex_call_returns_the_notice_and_starts_nothing(fresh_install, monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(server, "_delegate_observing_the_caller",
                        lambda *args, **kwargs: started.append(True))

    first = asyncio.run(server.modelquad_delegate("review", "Review it.", str(tmp_path), files=None))

    payload = _payload(first)
    assert payload["status"] == "notice_first" and payload["codex_started"] is False
    assert payload["notice"] == list(disclosure.DATA_FLOW)
    assert "Show the user the notice" in payload["next"]
    assert started == []
    assert disclosure.notice_shown() is True
    assert server._notice_first() is None, "shown once, then calls run"


def test_the_handoff_shows_the_notice_first_too(fresh_install, tmp_path):
    result = asyncio.run(server.modelquad_handoff("review", "Review it.", str(tmp_path)))
    assert _payload(result)["status"] == "notice_first"


def test_an_install_that_printed_the_notice_counts(fresh_install, tmp_path):
    assert disclosure.notice_shown() is False
    target = tmp_path / ".claude"
    target.mkdir()
    assert cli.main(["install", "claude", "--target-dir", str(target), "--json"]) == 0
    assert disclosure.notice_shown() is True


def test_a_dry_run_install_does_not_count(fresh_install, tmp_path):
    target = tmp_path / ".claude"
    target.mkdir()
    assert cli.main(["install", "claude", "--target-dir", str(target), "--dry-run", "--json"]) == 0
    assert (fresh_install / disclosure.NOTICE_RECORD_NAME).exists() is False


def test_an_earlier_install_counts_without_a_record(fresh_install):
    # 0.3.0 printed the notice on every install that wrote the registration
    # backup, so an upgraded installation is not stopped again.
    (paths.state_root() / "host-registration-backup.json").write_text("{}", encoding="utf-8")
    assert disclosure.notice_shown() is True


def test_unreadable_state_reads_as_not_shown(monkeypatch):
    monkeypatch.setattr(disclosure, "notice_shown", REAL_NOTICE_SHOWN)

    def broken():
        raise OSError("no state folder")

    monkeypatch.setattr(disclosure, "_state_root", broken)
    assert disclosure.notice_shown() is False


def test_the_quota_refresh_tool_waits_for_the_notice_before_asking_codex(fresh_install, monkeypatch):
    from modelquad.usage import refresh

    asked = []
    real = refresh.refresh_all
    monkeypatch.setattr(refresh, "refresh_all",
                        lambda directory, **kwargs: asked.append(kwargs.get("providers")) or {})
    payload = server.modelquad_usage_refresh()
    assert asked == [["claude"]], "Codex's app-server waits for the notice"
    assert payload["skipped"] == {"codex": "notice_not_shown"} and "notice" in payload["note"]
    disclosure.record_notice_shown("first_call")
    asked.clear()
    payload = server.modelquad_usage_refresh()
    assert sorted(asked[0]) == ["claude", "codex"] and "skipped" not in payload
    assert real is not None
