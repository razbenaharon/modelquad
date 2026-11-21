# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The data-flow notice reaches a user before Codex receives anything.

``modelquad install`` prints it. The plugin can put ModelQuad in front of
someone who never ran it, so their first call that would start Codex returns
the notice instead, once. Showing a package starts nothing and does not spend
the notice.
"""

from __future__ import annotations

import anyio
import pytest

from modelquad import cli, disclosure, paths, review
from modelquad.mcp import server, worker_guard

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


class _Started:
    """Stands in for Codex: notes each request that passed every gate."""

    def __init__(self):
        self.requests = []

    def execute(self, request):
        from modelquad.execution.base import ExecutionResult, WorkerTaskStatus

        self.requests.append(request)
        return ExecutionResult(provider="codex", success=False, output="", exit_code=None,
                               task_status=WorkerTaskStatus.BLOCKED,
                               task_status_reason="codex_version_unsupported",
                               metadata={"worker_started": False})


def _delegate(folder):
    async def invoke():
        return await server.delegate(task_type="review", prompt="Review it.",
                                               working_directory=str(folder), timeout=60.0)

    return anyio.run(invoke)


def test_the_first_codex_call_returns_the_notice_and_starts_nothing(fresh_install, monkeypatch, tmp_path):
    executor = _Started()
    monkeypatch.setattr(server, "_executor", lambda: executor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    first = _delegate(tmp_path)

    payload = _payload(first)
    assert payload["status"] == "notice_first" and payload["codex_started"] is False
    assert payload["notice"] == list(disclosure.DATA_FLOW)
    assert "Show the user the notice" in payload["next"]
    # The person reads the notice itself, in full, right after the outcome line.
    lines = first.content[0].text.splitlines()
    assert lines[0].startswith("Codex was not started.")
    assert lines[1:1 + len(disclosure.DATA_FLOW)] == list(disclosure.DATA_FLOW)
    assert executor.requests == []
    assert disclosure.notice_shown() is True
    assert review.notice() is None, "shown once, then calls run"
    second = _payload(_delegate(tmp_path))
    assert second.get("status") != "notice_first" and len(executor.requests) == 1


def test_showing_a_package_neither_waits_for_the_notice_nor_spends_it(fresh_install, monkeypatch, tmp_path):
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    async def invoke():
        return await server.review_package(task_type="review", prompt="Review it.",
                                                     working_directory=str(tmp_path), files=["a.py"])

    shown = _payload(anyio.run(invoke))
    assert shown.get("status") != "notice_first" and shown["files"][0]["path"] == "a.py"
    assert disclosure.notice_shown() is False, "the first Codex call still shows the notice"


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


def test_an_earlier_install_saw_the_first_version_and_sees_the_new_one_once(fresh_install):
    # 0.3.0 printed the notice on every install that wrote the registration
    # backup: that user saw version 1, and is shown the current one once.
    (paths.state_root() / "host-registration-backup.json").write_text("{}", encoding="utf-8")
    assert disclosure.shown_version() == 1
    assert disclosure.notice_shown() is False

    payload = review.notice()

    assert payload["status"] == "notice_first" and payload["notice_changed"] is True
    assert "changed since the user last saw it" in payload["next"]
    assert payload["notice"] == list(disclosure.DATA_FLOW)
    assert review.headline(payload).startswith("Codex was not started. ModelQuad's notice")
    assert disclosure.notice_shown() is True and review.notice() is None


def test_a_record_without_a_version_is_the_first_version(fresh_install):
    (fresh_install / disclosure.NOTICE_RECORD_NAME).write_text(
        '{"schema": 1, "via": "first_call", "shown_at": "2026-11-01T00:00:00+00:00"}',
        encoding="utf-8")

    assert disclosure.shown_version() == 1 and disclosure.notice_shown() is False


def test_the_record_keeps_the_version_shown(fresh_install):
    import json

    disclosure.record_notice_shown("first_call")

    record = json.loads((fresh_install / disclosure.NOTICE_RECORD_NAME).read_text(encoding="utf-8"))
    assert record["notice_version"] == disclosure.NOTICE_VERSION
    assert disclosure.notice_shown() is True


def test_a_first_showing_does_not_say_changed(fresh_install):
    payload = review.notice()

    assert "notice_changed" not in payload and "Show the user the notice" in payload["next"]


def test_unreadable_state_reads_as_not_shown(monkeypatch):
    monkeypatch.setattr(disclosure, "notice_shown", REAL_NOTICE_SHOWN)

    def broken():
        raise OSError("no state folder")

    monkeypatch.setattr(disclosure, "_state_root", broken)
    assert disclosure.notice_shown() is False
