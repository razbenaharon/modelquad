# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""What a user is told before ModelQuad sends their work anywhere.

Two texts, kept here so every surface prints the same words:

* ``DATA_FLOW`` is shown before first use (``modelquad onboard`` and a
  ``modelquad install`` that writes the Claude Code integration). It says who
  decides to ask Codex and who approves it, what ModelQuad reads and sends
  through Codex, where answers go, the background quota read and its off
  switch, where each provider's training settings live, that employer or
  client code needs the owner's permission, and that account limits are the
  user's risk.
* ``BACKGROUND_WORK`` is the separate consent for the opt-in job runner, which
  keeps delegated work running after the chat is closed.

Plain text, no formatting: the CLI prints it as is.
"""

from __future__ import annotations

from typing import Callable

CHATGPT_DATA_CONTROLS = "https://chatgpt.com/#settings/DataControls"
CLAUDE_PRIVACY_SETTINGS = "https://claude.ai/settings/data-privacy-controls"

DATA_FLOW = (
    "Before you use ModelQuad",
    "- Claude offers to ask Codex for a read-only opinion when it would help (with",
    "  modelquad settings proactive on, it may decide on its own). Claude Code",
    "  asks you before each call, also in auto and bypass-permissions modes.",
    "- For each call, ModelQuad reads the files the request names, inside the",
    "  working folder only. It skips credential files and replaces secret-looking",
    "  values. Your Codex CLI then sends the task and that text to OpenAI, under",
    "  your OpenAI account and OpenAI's terms. Codex itself gets no file, command,",
    "  web or MCP tools.",
    "- Codex's answer goes back to Claude Code, which sends it to Anthropic as part of",
    "  your conversation, under your Anthropic account and Anthropic's terms.",
    "- Codex's usage percentages and reset times are shown in Claude Code too, and",
    "  so reach Anthropic. While Claude Code runs ModelQuad, ModelQuad asks your",
    "  Codex CLI for them about every 4 minutes, with no prompt. To stop that, set",
    "  the environment variable MODELQUAD_CODEX_QUOTA_REFRESH to 0 and restart",
    "  Claude Code.",
    "- Each provider's terms forbid using its output to train the other's models.",
    "  We recommend turning off model training with both while you use ModelQuad:",
    f"    ChatGPT data controls: {CHATGPT_DATA_CONTROLS}",
    f"    Claude privacy settings: {CLAUDE_PRIVACY_SETTINGS}",
    "  Opting out does not cover feedback you submit or conversations flagged for",
    "  safety review.",
    "- If the code belongs to an employer or a client, check that you may send it to",
    "  OpenAI before you let Claude Code use ModelQuad on it.",
    "- Anthropic and OpenAI can limit or suspend accounts. That risk is yours.",
)

BACKGROUND_WORK = (
    "Keep delegated work running after you close the chat",
    "- A task you hand to Codex keeps running on this computer after Claude Code",
    "  closes, without a window, until it finishes or reaches its time limit.",
    "- It uses your Codex plan's usage while you are not watching.",
    "- Its answer is kept on this computer for up to 7 days, until you collect it.",
    "- Turn it off at any time with: modelquad settings job-runner off",
)

#: The words a person types to give the background-work consent.
BACKGROUND_CONSENT_ANSWER = "yes"


def print_lines(lines: tuple[str, ...], print_fn: Callable[..., None] = print) -> None:
    for line in lines:
        print_fn(line)
