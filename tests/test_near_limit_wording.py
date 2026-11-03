# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The near-limit note informs; it never proposes moving the work to Codex.

Owner decision D1 (2026-11-03): when Claude is near its limit, ModelQuad says
so with the facts and leaves the choice to the user: wait for the reset, or
ask Codex for a read-only review. Over a grid of quota states for both
providers (fresh, stale, unknown; below and above the thresholds), no status
line, MCP reply, tool title or description, skill text or server-instructions
text may carry a phrase that reads as handing the task over or switching
because of quota, and the note never starts a job.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from modelquad import assistance
from modelquad.adapters.server_instructions import server_instructions
from modelquad.config import Config
from modelquad.execution import ExecutionManager
from modelquad.mcp import server as mcp
from modelquad.models import Freshness, FreshnessThresholds, ProviderStatus
from modelquad.service import db

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = FreshnessThresholds()

#: Phrases that read as moving or continuing the work on Codex because of quota.
BANNED = (
    "hand this task", "hand off to codex", "hand it to codex", "switch", "route",
    "continue on codex", "take over", "failover", "save quota", "save usage",
    "keep coding", "continue after", "stretch", "move the task",
)


def _banned_in(text: str) -> list[str]:
    folded = text.casefold()
    return [phrase for phrase in BANNED if phrase in folded]


def _status(name, now, *, five=None, seven=None, age=None, five_reset=80, seven_reset=60 * 24 * 3):
    """A normalized status; ``age`` None means no reading at all (unknown)."""
    metadata = {"source": "claude_statusline" if name == "claude" else "codex_app_server"}
    if age is not None:
        if five is not None:
            metadata["five_hour_used_percentage"] = five
            metadata["five_hour_resets_at"] = (now + timedelta(minutes=five_reset)).isoformat()
        if seven is not None:
            metadata["seven_day_used_percentage"] = seven
            metadata["seven_day_resets_at"] = (now + timedelta(minutes=seven_reset)).isoformat()
    used = [value for value in (five, seven) if value is not None and age is not None]
    observed = None if age is None else now - age
    return ProviderStatus(
        name=name, available=True,
        remaining_percentage=100.0 - max(used) if used else None,
        observed_at=observed, freshness=Freshness.UNKNOWN if age is None else Freshness.FRESH,
        metadata=metadata,
    )


FRESH, STALE = timedelta(0), timedelta(hours=2)
CLAUDE = {
    "unknown": {},
    "fresh-low": {"five": 50, "seven": 40, "age": FRESH},
    "fresh-5h-over": {"five": 85, "seven": 40, "age": FRESH},
    "fresh-5h-over-resets-soon": {"five": 92, "seven": 40, "age": FRESH, "five_reset": 10},
    "fresh-5h-full": {"five": 100, "seven": 60, "age": FRESH},
    "fresh-week-over": {"five": 50, "seven": 95, "age": FRESH},
    "fresh-both-over-resets-soon": {"five": 95, "seven": 93, "age": FRESH, "five_reset": 10},
    "stale-5h-over": {"five": 88, "age": STALE, "five_reset": 40},
    "stale-low": {"five": 20, "seven": 10, "age": STALE, "five_reset": 40},
}
CODEX = {
    "absent": None,
    "unknown": {},
    "fresh-low": {"five": 30, "age": FRESH, "five_reset": 180},
    "fresh-high": {"five": 95, "age": FRESH},
    "stale-low": {"five": 30, "seven": 20, "age": STALE, "five_reset": 40},
}
GRID = list(itertools.product(CLAUDE, CODEX))


def _statuses(claude, codex, now):
    statuses = [_status("claude", now, **CLAUDE[claude])]
    if CODEX[codex] is not None:
        statuses.append(_status("codex", now, **CODEX[codex]))
    return statuses


@pytest.fixture
def tripwires(tmp_path, monkeypatch):
    """Every way to start work raises; the list records any attempt."""
    path = tmp_path / "state.db"
    db.connect_and_init(path).close()
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(mcp, "load_config", lambda *a: Config())
    monkeypatch.setattr(mcp, "cached_catalog", lambda *a: None)
    started = []

    def tripwire(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("a near-limit note started work")

    async def delegate_tripwire(**kwargs):
        tripwire(**kwargs)

    monkeypatch.setattr(mcp, "_delegate_payload", tripwire)
    monkeypatch.setattr(mcp, "start_job", tripwire)
    monkeypatch.setattr(mcp, "_build_execution_manager", tripwire)
    monkeypatch.setattr(mcp, "modelquad_delegate", delegate_tripwire)
    monkeypatch.setattr(ExecutionManager, "delegate", tripwire)
    return started


class _DecliningCtx:
    def __init__(self):
        self.asked = []

    async def elicit(self, message, schema):
        self.asked.append(message)
        return SimpleNamespace(action="decline", data=None)


@pytest.mark.parametrize("claude,codex", GRID)
def test_no_surface_proposes_moving_the_work_and_nothing_starts(claude, codex, tripwires, monkeypatch):
    now = datetime.now(timezone.utc)
    statuses = _statuses(claude, codex, now)

    suggestion = assistance.handoff_suggestion(statuses, THRESHOLDS, now=now)
    texts = [assistance.statusline_text(statuses, THRESHOLDS, now=now),
             assistance.statusline_segment(statuses, THRESHOLDS, now=now)]
    if suggestion is not None:
        assert suggestion["launches"] is False
        assert "You decide." in suggestion["message"]
        texts.append(json.dumps(suggestion))

    providers = [SimpleNamespace(name=s.name, get_status=lambda s=s: s) for s in statuses]
    monkeypatch.setattr(mcp, "build_providers", lambda *a, **k: providers)
    payload = mcp._provider_status_payload()
    assert (payload["handoff_suggestion"] or {}).get("launches", False) is False
    texts.append(json.dumps(payload))
    texts.append(mcp._handoff_message("review", "C:/work/app")[0])

    monkeypatch.setattr(mcp, "MIN_HUMAN_ANSWER_SECONDS", 0.0)
    ctx = _DecliningCtx()
    reply = asyncio.run(mcp.modelquad_handoff("review", "task", "C:/work/app", ctx=ctx))
    assert reply.structured_content["started"] is False
    texts += [json.dumps(reply.structured_content), *ctx.asked]
    texts += [block.text for block in reply.content]

    for text in texts:
        assert _banned_in(text) == [], text
    assert tripwires == []


def test_the_grid_reaches_every_kind_of_note():
    now = datetime.now(timezone.utc)
    kinds = {(assistance.handoff_suggestion(_statuses(c, x, now), THRESHOLDS, now=now) or {}).get("to")
             for c, x in GRID}
    assert kinds == {None, "wait", "codex"}


@pytest.mark.parametrize("proactive", [False, True])
def test_server_instructions_never_propose_moving_the_work(proactive):
    text = server_instructions(proactive)
    assert _banned_in(text) == []
    assert "near its limit" in text and "Only if they ask, use `modelquad_handoff`." in text
    assert len(text) < 4096


def test_tool_titles_descriptions_and_the_handoff_form_never_propose_moving_the_work():
    texts = [mcp.HANDOFF_QUESTION, mcp._FORM_UNAVAILABLE_STEP, mcp.NOTICE_FIRST_NEXT,
             json.dumps(mcp.HandoffApproval.model_json_schema())]
    tools = mcp.server._tool_manager._tools
    for name in ("modelquad_status", "modelquad_handoff", "modelquad_handoff_wait",
                 "modelquad_handoff_confirm", "modelquad_usage_refresh"):
        texts += [tools[name].title or "", tools[name].description or ""]
    from modelquad.mcp import handoff_widget
    texts.append(handoff_widget.HTML)
    for text in texts:
        assert _banned_in(text) == [], text


def test_skill_texts_never_propose_moving_the_work():
    skills = [*(ROOT / "plugin" / "skills").glob("*/SKILL.md"),
              *(ROOT / "src" / "modelquad" / "adapters" / "templates" / "claude" / "skills").glob("*/SKILL.md")]
    assert skills
    for path in skills:
        assert _banned_in(path.read_text(encoding="utf-8")) == [], path
