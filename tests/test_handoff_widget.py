# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The handoff approval page, for a Claude Code that renders MCP Apps.

Nothing starts until the page's Approve button answers; Claude can only wait.
"""

import asyncio
import json

import pytest

from modelquad.mcp import handoff_widget
from modelquad.mcp import server as mcp


@pytest.fixture(autouse=True)
def fresh_widget_store():
    handoff_widget.reset()
    yield
    handoff_widget.reset()


def _apps_client(**kwargs):
    from mcp.client import Client
    from mcp.client.extension import advertise
    from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

    return Client(mcp.server, mode="legacy",
                  extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})], **kwargs)


@pytest.fixture
def started(monkeypatch):
    calls = []

    async def fake_delegate(**kwargs):
        calls.append({**kwargs, "route": mcp._approval_route.get()})
        return mcp._plain_result({"started": True})

    monkeypatch.setattr(mcp, "modelquad_delegate", fake_delegate)
    monkeypatch.setattr(mcp, "WIDGET_WAIT_SECONDS", 0.2)
    return calls


def _handoff(client, tmp_path):
    return client.call_tool("modelquad_handoff", {
        "task_type": "review", "prompt": "check it", "working_directory": str(tmp_path),
        "files": ["notes.md"]})


def test_the_page_answer_is_the_only_way_to_start(tmp_path, started):
    (tmp_path / "notes.md").write_bytes(b"hello\n")
    asked = []

    async def form(context, params):
        asked.append(params)

    async def run():
        async with _apps_client(elicitation_callback=form) as client:
            held = (await _handoff(client, tmp_path)).structured_content
            waiting = (await client.call_tool("modelquad_handoff_wait",
                                              {"pending_id": held["pending_id"]})).structured_content
            confirm = (await client.call_tool("modelquad_handoff_confirm",
                                              {"pending_id": held["pending_id"], "approve": True})).structured_content
            again = (await client.call_tool("modelquad_handoff_confirm",
                                            {"pending_id": held["pending_id"], "approve": True})).structured_content
            done = (await client.call_tool("modelquad_handoff_wait",
                                           {"pending_id": held["pending_id"]})).structured_content
            replay = (await client.call_tool("modelquad_handoff_wait",
                                             {"pending_id": held["pending_id"]})).structured_content
            return held, waiting, confirm, again, done, replay

    held, waiting, confirm, again, done, replay = asyncio.run(run())
    assert asked == []  # the page, not the form
    assert (held["started"], held["approval"]) == (False, "widget_pending")
    assert held["sent_files_preview"] == [{"path": "notes.md", "bytes": 6}]
    assert held["codex_account"].startswith("Codex account:")
    assert waiting["approval"] == "waiting"
    assert confirm == {"recorded": True, "outcome": "recorded"}
    assert again == {"recorded": False, "outcome": "already_answered"}
    assert done == {"started": True}
    assert replay["started"] is False and replay["approval"] == "unknown_or_expired"
    (call,) = started
    assert call["files"] == ["notes.md"] and call["prompt"] == "check it" and call["route"] == "handoff"


def test_decline_on_the_page_starts_nothing(tmp_path, started):
    async def run():
        async with _apps_client() as client:
            held = (await _handoff(client, tmp_path)).structured_content
            await client.call_tool("modelquad_handoff_confirm",
                                   {"pending_id": held["pending_id"], "approve": False})
            return (await client.call_tool("modelquad_handoff_wait",
                                           {"pending_id": held["pending_id"]})).structured_content

    assert asyncio.run(run())["approval"] == "decline"
    assert started == []


def test_a_client_that_shows_no_widget_cannot_answer_for_the_user(tmp_path, started, monkeypatch):
    """Only a client that renders the page keeps the confirm tool from the model."""
    from mcp.client import Client

    key = handoff_widget.hold({"task_type": "review"})

    async def run():
        async with Client(mcp.server, mode="legacy") as client:
            return (await client.call_tool("modelquad_handoff_confirm",
                                           {"pending_id": key, "approve": True})).structured_content

    assert asyncio.run(run()) == {"recorded": False, "outcome": "client_shows_no_widget"}
    assert handoff_widget.take(key, 0.0) == ("waiting", None)


def test_the_confirm_tool_is_for_the_page_only_and_the_handoff_names_the_page():
    tools = {tool.name: tool for tool in asyncio.run(mcp.server.list_tools())}
    assert tools["modelquad_handoff_confirm"].meta["ui"]["visibility"] == ["app"]
    assert tools["modelquad_handoff"].meta["ui"]["resourceUri"] == handoff_widget.URI
    assert not (tools["modelquad_handoff_wait"].meta or {}).get("ui")


def test_the_page_is_served_as_an_mcp_app():
    from mcp.client import Client

    async def run():
        async with Client(mcp.server, mode="legacy") as client:
            return await client.read_resource(handoff_widget.URI)

    content = asyncio.run(run()).contents[0]
    assert content.mime_type == "text/html;profile=mcp-app"
    assert "modelquad_handoff_confirm" in content.text
    # The page writes what it shows as text, never as markup.
    assert "innerHTML" not in content.text


def test_an_unanswered_request_expires(monkeypatch):
    key = handoff_widget.hold({"task_type": "review"})
    monkeypatch.setattr(handoff_widget, "PENDING_SECONDS", 0.0)
    assert handoff_widget.decide(key, True) == "unknown_or_expired"
    assert handoff_widget.take(key, 0.0) == ("unknown_or_expired", None)


def test_a_worker_can_neither_answer_nor_wait(monkeypatch):
    monkeypatch.setenv("MODELQUAD_WORKER", "1")
    key = handoff_widget.hold({"task_type": "review"})
    assert mcp.modelquad_handoff_confirm(key, True).structured_content["refused"] is True
    result = asyncio.run(mcp.modelquad_handoff_wait(key))
    assert result.structured_content["refused"] is True


def test_the_doctor_tool_says_how_this_session_approves_a_handoff(monkeypatch):
    from mcp.client import Client

    monkeypatch.setattr(mcp, "run_doctor", lambda *a, **k: type("R", (), {"to_dict": lambda self: {}})())

    async def run(client):
        async with client:
            result = await client.call_tool("modelquad_doctor", {})
            return json.loads(result.content[0].text)["this_session"]

    assert asyncio.run(run(_apps_client())) == {
        "handoff_window_shown": True, "handoff_approval": "window", "client_renders_mcp_apps": True}
    assert asyncio.run(run(Client(mcp.server, mode="legacy")))["handoff_approval"] == "form"
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    assert asyncio.run(run(Client(mcp.server, mode="legacy")))["handoff_approval"] == "approval_card"
    # The desktop app renders the window but its Approve never arrived
    # (live test, 2026-10-25): there the card stays the answer.
    assert asyncio.run(run(_apps_client())) == {
        "handoff_window_shown": False, "handoff_approval": "approval_card", "client_renders_mcp_apps": True}


def test_in_the_desktop_app_the_card_wins_even_where_the_window_renders(tmp_path, started, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    (tmp_path / "notes.md").write_bytes(b"hello\n")

    async def run():
        async with _apps_client() as client:
            return (await _handoff(client, tmp_path)).structured_content

    assert asyncio.run(run()) == {"started": True}
    (call,) = started
    assert call["route"] == "delegate"


def test_the_page_says_when_an_answer_did_not_arrive():
    # A refused or failed call is not "no longer open": the request may still be.
    assert "Your answer did not reach ModelQuad" in handoff_widget.HTML


def test_approve_covers_the_contents_the_window_showed(tmp_path, monkeypatch):
    """The delegate call runs after Approve, possibly minutes later; it carries
    the digests taken when the window showed the files, not fresh ones."""
    seen = []

    async def fake_delegate(**kwargs):
        seen.append(mcp._approved_digests.get())
        return mcp._plain_result({"started": True})

    monkeypatch.setattr(mcp, "modelquad_delegate", fake_delegate)
    monkeypatch.setattr(mcp, "WIDGET_WAIT_SECONDS", 0.2)
    (tmp_path / "notes.md").write_bytes(b"hello\n")

    async def run():
        async with _apps_client() as client:
            held = (await _handoff(client, tmp_path)).structured_content
            (tmp_path / "notes.md").write_bytes(b"changed\n")
            await client.call_tool("modelquad_handoff_confirm",
                                   {"pending_id": held["pending_id"], "approve": True})
            return (await client.call_tool("modelquad_handoff_wait",
                                           {"pending_id": held["pending_id"]})).structured_content

    assert asyncio.run(run()) == {"started": True}
    from modelquad.execution import noshell
    (digests,) = seen
    assert digests["notes.md"] == noshell.sent_entry("notes.md", "hello\n")["sha256"]
    assert mcp._approved_digests.get() is None
