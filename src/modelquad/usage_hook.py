# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Claude Code's status-line hook: the one implementation behind ``usage-hook``.

Both the light installed entry (``cli_entry``) and ``cli.main`` run this, so
the status line writes the same files and prints the same text whichever
path reached it. It deliberately imports no router, registry or YAML loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hookinput import read_hook_payload
from .usage.base import write_usage_snapshot
from .usage.claude import parse_claude_statusline_payload, publish_claude_push


def record_claude_payload(payload: Any, usage_directory: Path) -> dict:
    """Record one Claude status-line payload; return the Claude usage snapshot."""

    # The same payload that carries quota is the only LIVE, session-scoped
    # channel through which Claude Code names the model it is running.
    # `record_host_push` is the ephemeral boundary: it keeps a handful of
    # closed-vocabulary fields and drops everything else, including the
    # transcript path and workspace path this payload is known to carry. It
    # must never break the status line.
    try:
        from .hostobs import record_host_push

        record_host_push("claude_code", payload)
    except Exception:  # noqa: BLE001
        pass
    # Claude Code renders its status line constantly, including in contexts
    # where the payload carries no rate_limits at all. Writing those
    # unconditionally would erase the only quota reading Claude ever gives us,
    # and nothing can re-fetch it -- there is no Claude command that reports
    # rate limits.
    # Read, compare and write happen under the snapshot's lock: a second
    # Claude Code session, or an idle one re-rendering figures it cached
    # earlier, must not put a lower figure over a higher one for the same
    # window. See ``usage.claude.merge_claude_push``.
    target = usage_directory / "usage-claude.json"
    snapshot = parse_claude_statusline_payload(payload)
    try:
        publish_claude_push(target, snapshot)
    except OSError:
        # Persistence failures must not break the parent client's status
        # line. Atomic replacement leaves the previous snapshot intact.
        pass
    return snapshot


PACE_FILE_NAME = "quota-pace.json"


def _statuses(usage_directory: Path):
    from .providers.claude import ClaudeProvider
    from .providers.codex import CodexProvider

    return (ClaudeProvider(usage_directory=usage_directory).get_status(),
            CodexProvider(usage_directory=usage_directory).get_status())


def _pace(usage_directory: Path, statuses, thresholds, now) -> dict:
    """Keep the first reading of each current 5-hour window; return them.

    Best-effort and display-only: a pace hint needs two readings, and this
    small file is where the earlier one waits. Any failure means no hint.
    """
    from .assistance import next_pace_readings

    target = usage_directory / PACE_FILE_NAME
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    readings = next_pace_readings(stored, statuses, thresholds, now=now)
    if readings != stored:
        try:
            write_usage_snapshot(target, readings)
        except OSError:
            pass
    return readings


def status_line_text(usage_directory: Path, claude_snapshot: dict, *, segment: bool = False) -> str:
    """Claude Code's status line: both providers' quota from the local readings.

    Claude's reading was just written by this same call; Codex's is the last
    app-server reading in the cache, which the MCP server refreshes in the
    background (``usage/background.py``) and `usage refresh` refreshes on
    demand. This call never probes Codex itself. Missing data is said in
    plain words or as "unknown", never 0%. Display only: it cannot start a
    job. ``segment`` prints only the compact part a user adds to a status
    line of their own. It must never break the status line, so any failure
    falls back to the Claude-only line.
    """
    try:
        from datetime import datetime, timezone

        from .assistance import statusline_segment, statusline_text
        from .models import FreshnessThresholds

        now = datetime.now(timezone.utc)
        thresholds = FreshnessThresholds()
        statuses = _statuses(usage_directory)
        pace = _pace(usage_directory, statuses, thresholds, now)
        render = statusline_segment if segment else statusline_text
        return render(statuses, thresholds, now=now, pace=pace)
    except Exception:  # noqa: BLE001
        five = claude_snapshot.get("five_hour_used_percentage")
        seven = claude_snapshot.get("seven_day_used_percentage")
        five_text = f"{100 - five:.0f}% left" if isinstance(five, (int, float)) else "unknown"
        if segment:
            return f"Claude 5h {five_text}"
        seven_text = f"{100 - seven:.0f}% left" if isinstance(seven, (int, float)) else "unknown"
        return f"Claude 5h {five_text} / 7d {seven_text}"


def run(*, usage_directory: Path, quiet: bool, segment: bool = False) -> int:
    """Read the payload from stdin, record it, and print the status line.

    ``quiet`` records exactly the same files and prints nothing; ``segment``
    prints only the compact ModelQuad part (see :func:`status_line_text`).
    """

    try:
        payload = read_hook_payload()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    snapshot = record_claude_payload(payload, Path(usage_directory))
    text = status_line_text(Path(usage_directory), snapshot, segment=segment)
    if not quiet:
        print(text)
    return 0
