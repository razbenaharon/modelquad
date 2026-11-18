# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""What a user is told before ModelQuad sends their work anywhere.

One text, kept here so every surface prints the same words:

``DATA_FLOW`` is shown before first use (a ``modelquad install`` that writes
the Claude Code integration, or the first Codex call). It says who decides to
ask Codex and who approves it, what ModelQuad reads and sends through Codex,
where answers go, where each provider's training settings live, that employer
or client code needs the owner's permission, and that account limits are the
user's risk.

Plain text, no formatting: the CLI prints it as is.
"""

from __future__ import annotations

from typing import Callable

CHATGPT_DATA_CONTROLS = "https://chatgpt.com/#settings/DataControls"
CLAUDE_PRIVACY_SETTINGS = "https://claude.ai/settings/data-privacy-controls"

DATA_FLOW = (
    "Before you use ModelQuad",
    "- When it would help, Claude asks Codex for a read-only second opinion on",
    "  its own (with modelquad settings proactive off, it offers instead).",
    "  Claude Code asks you before each call, also in auto and bypass-permissions",
    "  modes, unless you turn on the standing grant for that folder or for every",
    "  folder (modelquad settings auto on).",
    "- For each call, ModelQuad reads the files the request names, inside the",
    "  working folder only. It refuses a call that names a credential file (a check",
    "  by file name, which cannot be complete) and replaces secret-looking",
    "  values. Your Codex CLI then sends the task and that text to OpenAI, under",
    "  your OpenAI account and OpenAI's terms. Codex runs read-only in an empty",
    "  folder with its file, command, web and MCP tool features switched off.",
    "- Codex's answer goes back to Claude Code, which sends it as part of your",
    "  conversation to Anthropic, or to the cloud provider you set Claude Code up to",
    "  use, under your account and that provider's terms. So do the names and sizes",
    "  of the files that were sent, the token counts Codex reported for the call,",
    "  and this repository's Codex setting.",
    "- OpenAI's terms forbid using its output to develop models that compete with",
    "  it, and Anthropic's forbid using Claude's output to train any AI model",
    "  without its permission. Turn off model training with both while you use",
    "  ModelQuad:",
    f"    ChatGPT data controls: {CHATGPT_DATA_CONTROLS}",
    f"    Claude privacy settings: {CLAUDE_PRIVACY_SETTINGS}",
    "  Opting out does not cover feedback you submit or conversations flagged for",
    "  safety review. Do not use ModelQuad's answers or its record as training data.",
    "- If the code belongs to an employer or a client, check that you may send it to",
    "  OpenAI and Anthropic before you let Claude Code use ModelQuad on it.",
    "- Do not run ModelQuad unattended: every Codex call should come from a Claude",
    "  Code session a person started for their own work. Do not script or schedule",
    "  ModelQuad, run it in CI, or run it for other people. OpenAI recommends",
    "  API-key sign-in for programmatic Codex use.",
    "- Codex's answers can be wrong, and code in them may be under other licences.",
    "- ModelQuad does not change any usage limit.",
    "- Anthropic and OpenAI can limit or suspend accounts. That risk is yours.",
)

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
