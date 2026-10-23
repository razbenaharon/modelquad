# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The MCP server entry every host adapter writes.

The template carries the entry's shape (its key, its ``args``); ``command``
comes from ``modelquad.hostruntime``, which owns the rule for what a host
launches (the bare ``modelquad-mcp`` that pip puts on PATH).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..branding import MCP_SERVER_NAME


def mcp_entry(template_dir: Path) -> dict[str, object]:
    """The MCP entry to write, with ``command`` bound to the owning runtime."""

    from ..hostruntime import mcp_launch_command

    payload = json.loads(
        (template_dir / "mcp" / "mcp-servers.json").read_text(encoding="utf-8")
    )
    entry = dict(payload["mcpServers"][MCP_SERVER_NAME])
    entry["command"] = mcp_launch_command()
    return entry
