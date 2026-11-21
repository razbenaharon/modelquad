# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""R10: the installed Claude Code instructions stay short, safe and accurate.

These checks inspect the shipped instruction templates and the registered MCP
schema; they launch no workers. Native Host loading is checked separately.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re

import pytest

from modelquad.mcp import server as mcp_server


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "src/modelquad/adapters/templates"
RELATIVE_FILES = (
    "claude/skills/modelquad/SKILL.md",
)
FILES = tuple(TEMPLATE_ROOT / relative for relative in RELATIVE_FILES)


def _instruction_sources():
    """Every instruction text shipped: the skill, and the MCP server's instructions
    in both proactive settings (they replaced the old global CLAUDE.md block)."""
    from modelquad.adapters.server_instructions import server_instructions

    for path in FILES:
        yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")
    yield "server instructions (default)", server_instructions(False)
    yield "server instructions (proactive)", server_instructions(True)


SOURCES = tuple(_instruction_sources())
# The rewritten companion instructions are about 25 plain lines each; the
# named review moments take a few more (Claude Code's own cut is 4,096).
SIZE_CEILING_BYTES = 2400
FORBIDDEN_PROMISES = (
    "fix it for me",
    "apply the findings automatically",
)
FORBIDDEN_PHRASES = (
    "PROACTIVELY",
    "quota-aware",
    "provider routing",
    "use-codex",
    "second_opinion",
    "modelquad_route",
    "modelquad_context_",
    "modelquad_plan",
    "routing mode",
    "resource strategy",
)
REQUIRED_SENTENCE_FRAGMENTS = (
    "read-only",
    "asks the user before",
    "MODELQUAD_WORKER=1",
    "`review_package`",
    "`completion_request`",
    "`follows`",
)
EXCLUDED_CHECKPOINTS = {
    "every turn": r"\b(?:every|each)\s+turn\b",
    "every commit": r"\b(?:every|each)\s+commit\b",
    "after each fixed test": r"\b(?:every|each)\s+(?:fixed\s+test|test\s+fix)\b",
    "hooks": r"\bhooks?\b",
    "Stop": r"\bstop(?:\s+events?)?\b",
}


@pytest.fixture(scope="module")
def registered_names():
    return {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}


def _affirmative_review_checkpoints(text: str) -> set[str]:
    """Scan all clauses, independent of verb, order, case or tool formatting.

    Explicit exclusions and negative imperatives do not mandate a review.
    Clause boundaries keep an exclusion from masking a later instruction.
    """
    normalized = " ".join(text.replace("`", "").replace("_", " ").casefold().split())
    found = set()
    for clause in re.split(r"[.!?;]+|\b(?:but|however)\b", normalized):
        review = re.search(r"\b(?:reviews?|second\s+opinions?)\b", clause)
        if review is None:
            continue
        if re.match(r"\s*(?:not checkpoints|no (?:review|second opinion))\b", clause):
            continue
        if re.search(r"(?:^|,)\s*(?:never|do not|don't)\b", clause[:review.start()]):
            continue
        found.update(name for name, pattern in EXCLUDED_CHECKPOINTS.items()
                     if re.search(pattern, clause))
    return found


def _instruction_errors(text: str, registered: set[str]) -> list[str]:
    errors = []
    named = {name for name in re.findall(r"\bmodelquad_[a-z0-9_]+\b", text)
             if not name.endswith("_")}
    if named - registered:
        errors.append("unregistered tool")
    if re.search(r"`_[a-z0-9_]+`", text):
        errors.append("abbreviated tool")
    flat = " ".join(text.split())
    if any(phrase in flat.lower() for phrase in FORBIDDEN_PROMISES):
        errors.append("edit promise")
    for phrase in FORBIDDEN_PHRASES:
        if phrase.casefold() in flat.casefold():
            errors.append(f"forbidden phrase: {phrase}")
    for excluded in sorted(_affirmative_review_checkpoints(text)):
        errors.append(f"affirmative checkpoint: {excluded}")
    return errors


@pytest.mark.parametrize(("name", "text"), SOURCES, ids=[name for name, _text in SOURCES])
def test_each_template_has_safe_instructions(name, text, registered_names):
    assert _instruction_errors(text, registered_names) == []
    flat = " ".join(text.split())
    for fragment in REQUIRED_SENTENCE_FRAGMENTS:
        assert fragment in flat, (name, fragment)
    # Codex output is input to verify, never instructions to follow.
    assert re.search(r"verify", flat, re.I)
    assert re.search(r"not (?:as )?instructions", flat, re.I)


def _standing_source():
    from modelquad.adapters.server_instructions import server_instructions

    return "server instructions (standing grant)", server_instructions(True, standing=True)


# The standing-grant text says calls skip the per-call prompt, so it is held
# to the size ceiling but not to the "asks the user before" fragment.
SIZED_SOURCES = (*SOURCES, _standing_source())


@pytest.mark.parametrize(("name", "text"), SIZED_SOURCES, ids=[name for name, _text in SIZED_SOURCES])
def test_template_size_ceiling(name, text):
    assert len(text.encode("utf-8")) <= SIZE_CEILING_BYTES
    assert len(text.splitlines()) <= 30


def test_top_level_template_copies_are_gone():
    for relative in RELATIVE_FILES:
        assert not (ROOT / "adapters" / relative).exists(), relative


@pytest.mark.parametrize(("mutation", "expected_error"), (
    ("unknown_tool", "unregistered tool"),
    ("every_turn", "affirmative checkpoint: every turn"),
    ("before_every_commit", "affirmative checkpoint: every commit"),
    ("proactively", "forbidden phrase: PROACTIVELY"),
    ("quota_aware", "forbidden phrase: quota-aware"),
    ("use_codex", "forbidden phrase: use-codex"),
))
def test_instruction_checker_rejects_unsafe_synthetic_text(registered_names, mutation, expected_error):
    text = (TEMPLATE_ROOT / "claude/skills/modelquad/SKILL.md").read_text(encoding="utf-8")
    text += {
        "unknown_tool": "\n`modelquad_review_every_turn`\n",
        "every_turn": "\nOffer a second opinion after every turn.\n",
        "before_every_commit": "\nOffer review before every commit.\n",
        "proactively": "\nUse ModelQuad PROACTIVELY.\n",
        "quota_aware": "\nModelQuad is a quota-aware router.\n",
        "use_codex": "\nInvoke the use-codex skill.\n",
    }[mutation]
    assert expected_error in _instruction_errors(text, registered_names)


@pytest.mark.parametrize(("instruction", "excluded"), (
    ("Run review before every commit.", "every commit"),
    ("Offer a second opinion on every turn.", "every turn"),
    ("Start review after each fixed test.", "after each fixed test"),
    ("Request a review from hooks.", "hooks"),
    ("Perform review on Stop events.", "Stop"),
    ("Launch REVIEW before each commit.", "every commit"),
    ("Before every commit, arrange a second opinion.", "every commit"),
))
def test_whole_file_detector_handles_verbs_order_and_formatting(registered_names, instruction, excluded):
    text = (TEMPLATE_ROOT / "claude/skills/modelquad/SKILL.md").read_text(encoding="utf-8")
    assert f"affirmative checkpoint: {excluded}" in _instruction_errors(
        text + "\n" + instruction, registered_names,
    )


@pytest.mark.parametrize("instruction", (
    "Never ask for a second opinion after every turn.",
    "Do not offer review before every commit.",
    "On Stop, don't run review.",
    "No review after each fixed test.",
))
def test_checkpoint_detector_preserves_explicit_negative_instructions(instruction):
    assert _affirmative_review_checkpoints(instruction) == set()


@pytest.mark.parametrize("promise", FORBIDDEN_PROMISES)
def test_instruction_checker_rejects_edit_promises(registered_names, promise):
    text = (TEMPLATE_ROOT / "claude/skills/modelquad/SKILL.md").read_text(encoding="utf-8")
    assert "edit promise" in _instruction_errors(text + "\n" + promise, registered_names)


def test_instruction_checker_rejects_abbreviated_tool_names(registered_names):
    text = (TEMPLATE_ROOT / "claude/skills/modelquad/SKILL.md").read_text(encoding="utf-8")
    assert "abbreviated tool" in _instruction_errors(text + "\n`_update`", registered_names)


def test_the_mcp_server_hands_claude_code_its_instructions():
    """The guidance reaches Claude through the server, not through a global CLAUDE.md."""
    from modelquad.adapters.server_instructions import server_instructions

    assert mcp_server.server.instructions in (server_instructions(False), server_instructions(True))
    assert len(mcp_server.server.instructions) < 4096  # Claude Code cuts instructions past 4,096 characters
