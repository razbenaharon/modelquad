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
    "  Unless you turn on the standing grant for that folder or for every folder",
    "  (modelquad settings auto on), each call needs your approval in Claude Code.",
    "  In bypass-permissions mode Claude Code still held such a call back in our",
    "  tests (Claude Code 2.1.318). With the grant on, files go to OpenAI with no",
    "  prompt and no file list shown to you first.",
    "- For each call, ModelQuad reads the files the request names, inside the",
    "  working folder only. It refuses a call that names a credential file (a check",
    "  by file name, which cannot be complete) and replaces secret-looking",
    "  values. Your Codex CLI then sends the task and that text to OpenAI, under",
    "  your OpenAI account and OpenAI's terms. Codex runs read-only in an empty",
    "  folder with its file, command, web and MCP tool features switched off.",
    "  The secret screen works on names and shapes: a secret under an ordinary",
    "  name can still be sent. Do not name files that hold secrets.",
    "- Codex's answer goes back to Claude Code, which sends it as part of your",
    "  conversation to Anthropic, or to the cloud provider you set Claude Code up to",
    "  use, under your account and that provider's terms. So do the names and sizes",
    "  of the files that were sent, the token counts Codex reported for the call,",
    "  and this repository's Codex setting.",
    "- ModelQuad keeps a record of each call on this computer: file names, sizes,",
    "  Codex's findings (shortened, with paths removed) and your verdicts; never",
    "  the task text, file contents or the whole answer. modelquad review report",
    "  shows it.",
    "- OpenAI's terms forbid using its output to develop models that compete with",
    "  it, and Anthropic's forbid using Claude's output to train any AI model",
    "  without its permission. Turn off model training with both while you use",
    "  ModelQuad:",
    f"    ChatGPT data controls: {CHATGPT_DATA_CONTROLS}",
    f"    Claude privacy settings: {CLAUDE_PRIVACY_SETTINGS}",
    "  If Codex is signed in with an API key or a ChatGPT workspace, that",
    "  account's data settings apply instead.",
    "  Opting out does not cover feedback you submit or conversations flagged for",
    "  safety review. Do not use ModelQuad's answers or its record as training data.",
    "- If the code belongs to an employer or a client, check that you may send it to",
    "  OpenAI and Anthropic before you let Claude Code use ModelQuad on it.",
    "- Automated runs. Without the standing grant, a person approves each Codex",
    "  call in Claude Code, so a script cannot start Codex. With the grant and an",
    "  allow rule, a headless or scheduled Claude Code run can, and ModelQuad",
    "  cannot tell whether anyone is watching. To automate it under control: grant",
    "  one folder, give each run one defined task, limit its time and turns, read",
    "  the record (modelquad review report), and keep a way to stop it (modelquad",
    "  off). Headless Claude Code runs were tested; CI, other schedulers and an",
    "  API-key sign-in were not.",
    "- Your sign-ins are yours. OpenAI's and Anthropic's terms forbid sharing your",
    "  login or making your account available to anyone else: do not let other",
    "  people, or a service you run for them, use ModelQuad through your sign-ins.",
    "  OpenAI's terms also forbid programmatically extracting output; whether an",
    "  automated ModelQuad run falls under that is not settled. For automation",
    "  such as CI, OpenAI recommends API-key sign-in and says not to use a ChatGPT",
    "  sign-in in public repositories.",
    "- Codex's answers can be wrong, and code in them may be under other licences.",
    "- ModelQuad does not change any usage limit.",
    "- Anthropic and OpenAI can limit or suspend accounts. That risk is yours.",
)

def print_lines(lines: tuple[str, ...], print_fn: Callable[..., None] = print) -> None:
    for line in lines:
        print_fn(line)


#: Where this installation records that ``DATA_FLOW`` reached the user.
NOTICE_RECORD_NAME = "data-flow-notice.json"
#: The version of ``DATA_FLOW``. Raise it when a change alters what a user
#: relies on (where data goes, what is kept, what consent covers); a user who
#: saw an earlier version is shown the new one once, before the next Codex
#: call. A record without a version, or only the install evidence below, is
#: version 1: the text shown until 2026-11-18.
NOTICE_VERSION = 2
#: Written by every install that wrote the Claude Code integration, which
#: printed ``DATA_FLOW`` since 0.3.0: an installation that has it has shown it.
_INSTALL_EVIDENCE_NAME = "host-registration-backup.json"


def _state_root():
    from .paths import state_root

    return state_root()


def shown_version() -> int:
    """The newest ``DATA_FLOW`` version this installation's user was shown; 0 for none.

    The plugin can put ModelQuad in front of someone who never ran
    ``modelquad onboard`` or ``modelquad install``, where the notice is
    printed. Unreadable state reads as not shown: the cost is one extra
    showing of the notice, never a Codex call before it.
    """

    import json

    try:
        root = _state_root()
        record = root / NOTICE_RECORD_NAME
        if record.is_file():
            data = json.loads(record.read_text(encoding="utf-8"))
            version = data.get("notice_version", 1) if isinstance(data, dict) else 1
            return version if type(version) is int and version >= 1 else 1
        return 1 if (root / _INSTALL_EVIDENCE_NAME).is_file() else 0
    except Exception:  # noqa: BLE001 -- unknown state means show the notice
        return 0


def notice_shown() -> bool:
    """Whether the current ``DATA_FLOW`` has reached the user of this installation."""

    return shown_version() >= NOTICE_VERSION


def record_notice_shown(via: str) -> None:
    """Remember that the notice was shown (``via``: where), so it is shown once."""

    import json
    from datetime import datetime, timezone

    from .adapters.atomicwrite import atomic_write

    target = _state_root() / NOTICE_RECORD_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps({
        "schema": 1, "via": via, "shown_at": datetime.now(timezone.utc).isoformat(),
        "notice_version": NOTICE_VERSION,
    }, indent=2) + "\n")
