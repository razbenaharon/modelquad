# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Progress during a review is best-effort and carries no task material.

``server._Progress`` and ``server._heartbeat`` report while Codex runs; a
client that fails or stalls on a progress message never holds up or fails
the review.
"""

import time

import anyio
import pytest

from modelquad.mcp import server as mcp, worker_guard
from test_review_flow import FakeExecutor

PROMPT = "PROMPT-DO-NOT-LEAK"
FILENAME = "private-file-name.py"
ANSWER = "ANSWER-DO-NOT-LEAK"


class _Ctx:
    def __init__(self, raises=False, stalls=False):
        self.raises = raises
        self.stalls = stalls
        self.progress = []

    async def report_progress(self, progress, total=None, message=None):
        if self.raises:
            raise RuntimeError("progress unavailable")
        if self.stalls:
            await anyio.sleep(60)
        self.progress.append((progress, total, message))


@pytest.fixture
def project(tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / FILENAME).write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    return folder


def _review(project, ctx, executor, monkeypatch):
    monkeypatch.setattr(mcp, "_executor", lambda: executor)

    async def invoke():
        return await mcp.modelquad_delegate(task_type="review", prompt=PROMPT,
                                            working_directory=str(project), timeout=60.0,
                                            ctx=ctx, files=[FILENAME])

    return anyio.run(invoke)


def test_delegate_reports_ordered_content_free_progress(project, monkeypatch):
    executor = FakeExecutor(ANSWER)
    # Long enough for the heartbeat to announce the start and then a working line.
    executor.before_answer = lambda: time.sleep(1.4)
    monkeypatch.setattr(mcp, "PROGRESS_INTERVAL_SECONDS", 0.05)
    ctx = _Ctx()

    result = _review(project, ctx, executor, monkeypatch)

    assert result.content[0].text.splitlines()[0] == "Codex answered."
    messages = [message for _, _, message in ctx.progress]
    assert messages[0] == "Codex started"
    assert any(message.startswith("Codex is working (") for message in messages[1:-1])
    assert messages[-1] == "Codex answered"
    values = [value for value, _, _ in ctx.progress]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert all(secret not in message for secret in (PROMPT, FILENAME, ANSWER) for message in messages)


@pytest.mark.parametrize("ctx", [_Ctx(raises=True), _Ctx(stalls=True)], ids=["raises", "stalls"])
def test_progress_failures_never_hold_up_or_fail_the_review(project, monkeypatch, ctx):
    executor = FakeExecutor(ANSWER)
    executor.before_answer = lambda: time.sleep(1.2)
    started = time.monotonic()

    result = _review(project, ctx, executor, monkeypatch)

    assert result.content[0].text.splitlines()[0] == "Codex answered."
    assert result.structured_content["output"].startswith(ANSWER)
    assert ctx.progress == []
    # One stalled message is given up after a short wait, not the stall's 60 s.
    assert time.monotonic() - started < 10


def test_progress_without_a_context_is_a_no_op():
    progress = mcp._Progress(None)

    anyio.run(progress.report, "Codex started")

    assert progress.enabled is False and progress.value == 0
