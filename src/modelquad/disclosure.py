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
    "- Claude offers to ask Codex for a read-only second opinion when it would",
    "  help (with modelquad settings proactive on, it may decide on its own).",
    "  Claude Code asks you before each call, also in auto and bypass-permissions",
    "  modes.",
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
    "- Claude also sees the kind of each account (personal, work or unknown), never",
    "  the plan name, and this repository's Codex setting.",
    "- Each provider's terms forbid using its output to train models that compete",
    "  with it. Turn off model training with both while you use ModelQuad:",
    f"    ChatGPT data controls: {CHATGPT_DATA_CONTROLS}",
    f"    Claude privacy settings: {CLAUDE_PRIVACY_SETTINGS}",
    "  Opting out does not cover feedback you submit or conversations flagged for",
    "  safety review. Do not use ModelQuad's answers or history as training data.",
    "- If the code belongs to an employer or a client, check that you may send it to",
    "  OpenAI and Anthropic before you let Claude Code use ModelQuad on it.",
    "- Do not run ModelQuad unattended, in CI or for other people. OpenAI recommends",
    "  API-key sign-in for programmatic Codex use.",
    "- Codex's answers can be wrong, and code in them may be under other licences.",
    "- ModelQuad does not change any usage limit.",
    "- Anthropic and OpenAI can limit or suspend accounts. That risk is yours.",
)

BACKGROUND_WORK = (
    "Keep delegated work running after you close the chat",
    "- A task you send to Codex keeps running on this computer after Claude Code",
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


#: Where this installation records that ``DATA_FLOW`` reached the user.
NOTICE_RECORD_NAME = "data-flow-notice.json"
#: Written by every install that wrote the Claude Code integration, which
#: printed ``DATA_FLOW`` since 0.3.0: an installation that has it has shown it.
_INSTALL_EVIDENCE_NAME = "host-registration-backup.json"


def _state_root():
    from .paths import state_root

    return state_root()


def notice_shown() -> bool:
    """Whether ``DATA_FLOW`` has reached the user of this installation.

    The plugin can put ModelQuad in front of someone who never ran
    ``modelquad onboard`` or ``modelquad install``, where the notice is
    printed. Unreadable state reads as not shown: the cost is one extra
    showing of the notice, never a Codex call before it.
    """

    try:
        root = _state_root()
        return (root / NOTICE_RECORD_NAME).is_file() or (root / _INSTALL_EVIDENCE_NAME).is_file()
    except Exception:  # noqa: BLE001 -- unknown state means show the notice
        return False


def record_notice_shown(via: str) -> None:
    """Remember that the notice was shown (``via``: where), so it is shown once."""

    import json
    from datetime import datetime, timezone

    from .adapters.atomicwrite import atomic_write

    target = _state_root() / NOTICE_RECORD_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps({
        "schema": 1, "via": via, "shown_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n")
