# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Public brand constants and the identifiers derived from them.

The Python package and worker guard use the same ModelQuad name as every
user-visible surface. Everything is named here rather than spelled out at each
use site, so there is exactly one place to change and one place to read when
asking "what do we call ourselves on this surface".
"""

from __future__ import annotations

PRODUCT_NAME = "ModelQuad"
PRODUCT_SLUG = "modelquad"

# Distribution name on PyPI and in importlib.metadata.version() lookups.
DISTRIBUTION_NAME = "modelquad"

# The name Claude Code's configuration registers this MCP server under.
MCP_SERVER_NAME = "modelquad"

# The plugin's name and the name its MCP server has inside the plugin
# (plugin/.mcp.json). Claude Code names a plugin's tools
# mcp__plugin_<plugin>_<server>__<tool>.
PLUGIN_NAME = "modelquad"
PLUGIN_SERVER_NAME = "review"

# The four MCP tools, and the names they had before 0.5.0.
TOOL_NAMES = ("review_package", "delegate", "review_verdict", "status")
OLD_TOOL_NAMES = {name: f"modelquad_{name}" for name in TOOL_NAMES}
# The tool-name prefix Claude Code shows for each install.
PLUGIN_TOOL_PREFIX = f"mcp__plugin_{PLUGIN_NAME}_{PLUGIN_SERVER_NAME}__"
INSTALL_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
# Before 0.5.0 the plugin's server was named like the install's.
OLD_PLUGIN_SERVER_ID = f"mcp__plugin_{PLUGIN_NAME}_{MCP_SERVER_NAME}"


def tool_ids(tool: str) -> tuple[str, str]:
    """The ids Claude Code gives one tool: (plugin install, install without the plugin)."""
    return PLUGIN_TOOL_PREFIX + tool, INSTALL_TOOL_PREFIX + tool


def old_tool_ids(tool: str) -> tuple[str, str]:
    """The ids the same tool had before 0.5.0: (plugin install, install without the plugin)."""
    return f"{OLD_PLUGIN_SERVER_ID}__{OLD_TOOL_NAMES[tool]}", INSTALL_TOOL_PREFIX + OLD_TOOL_NAMES[tool]


def delegate_ids() -> frozenset[str]:
    """Every id the review tool can have in Claude Code. After an upgrade the
    package and Claude Code's cached copy of the plugin can be of different
    versions, so the plugin's server name before and since 0.5.0 can meet the
    tool's name before and since 0.5.0, in any combination."""
    servers = (PLUGIN_TOOL_PREFIX, OLD_PLUGIN_SERVER_ID + "__", INSTALL_TOOL_PREFIX)
    return frozenset(server + tool for server in servers for tool in ("delegate", OLD_TOOL_NAMES["delegate"]))


# Console entry points.
CLI_COMMAND = "modelquad"
MCP_COMMAND = "modelquad-mcp"

# Marker tag delimiting our managed region inside a host's own config file
# (CLAUDE.md). Changing this without migrating would orphan the previously
# written block instead of replacing it.
MANAGED_BLOCK_TAG = "modelquad"


def installed_version(default: str | None = None) -> str | None:
    """Return the installed distribution version.

    A version lookup is never important enough to fail a caller over. When no
    dist-info resolves, the package's own ``__version__`` is the fallback;
    without it an installed product reported ``version: n/a`` from
    /api/health and stamped no ``modelquad_version`` into DecisionTrace
    provenance. ``default`` is returned only when neither resolves.
    """

    from importlib import metadata

    try:
        return metadata.version(DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        pass
    try:
        from . import __version__

        return __version__
    except ImportError:
        return default
