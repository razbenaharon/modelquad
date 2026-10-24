# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
import asyncio
import time

from mcp.types import CallToolResult

from modelquad.mcp import server as mcp


class _Ctx:
    def __init__(self, raises=False):
        self.raises = raises
        self.progress = []

    async def report_progress(self, progress, total=None, message=None):
        if self.raises:
            raise RuntimeError("progress unavailable")
        self.progress.append((progress, total, message))


def _stub_presentation(monkeypatch):
    monkeypatch.setattr(mcp, "build_delegation_presentation", lambda *args, **kwargs: object())
    monkeypatch.setattr(mcp, "_decision_result", lambda *args, **kwargs: CallToolResult(content=[]))


def test_delegate_reports_ordered_content_free_progress(monkeypatch, tmp_path):
    prompt = "PROMPT-DO-NOT-LEAK"
    filename = "private-file-name.py"
    answer = "ANSWER-DO-NOT-LEAK"

    def fake_payload(*args, **kwargs):
        mcp._runner_packed({"sent": [{"bytes": 12}]})
        mcp._runner_notice("Codex started")
        time.sleep(0.03)
        return {"output": answer}

    monkeypatch.setattr(mcp, "_delegate_payload", fake_payload)
    monkeypatch.setattr(mcp, "DELEGATE_PROGRESS_INTERVAL_SECONDS", 0.005)
    _stub_presentation(monkeypatch)
    ctx = _Ctx()

    asyncio.run(mcp.modelquad_delegate("review", prompt, str(tmp_path), ctx=ctx, files=[filename]))

    messages = [message for _, _, message in ctx.progress]
    assert messages[0] == "Packed 1 file(s), 12 bytes for Codex"
    assert messages[1] == "Codex started"
    assert any(message.startswith("Codex is working (") for message in messages[2:-1])
    assert messages[-1] == "Codex answered"
    assert [value for value, _, _ in ctx.progress] == sorted(value for value, _, _ in ctx.progress)
    assert all(secret not in message for secret in (prompt, filename, answer) for message in messages)


def test_delegate_ignores_progress_delivery_failures(monkeypatch, tmp_path):
    def fake_payload(*args, **kwargs):
        mcp._runner_packed({"sent": []})
        mcp._runner_notice("Codex started")
        return {"output": "answer"}

    monkeypatch.setattr(mcp, "_delegate_payload", fake_payload)
    _stub_presentation(monkeypatch)

    result = asyncio.run(mcp.modelquad_delegate("review", "task", str(tmp_path), ctx=_Ctx(raises=True)))

    assert isinstance(result, CallToolResult)


def test_delegate_reports_task_text_only_when_no_file_was_packed(monkeypatch, tmp_path):
    def fake_payload(*args, **kwargs):
        mcp._runner_packed({"sent": []})
        mcp._runner_notice("Codex started")
        return {"output": "answer"}

    monkeypatch.setattr(mcp, "_delegate_payload", fake_payload)
    _stub_presentation(monkeypatch)
    ctx = _Ctx()

    asyncio.run(mcp.modelquad_delegate("review", "task", str(tmp_path), ctx=ctx))

    assert ctx.progress[0][2] == "Sending only the task text to Codex"
