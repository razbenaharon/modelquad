# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Public brand constants and the identifiers derived from them.

The Python package itself stays ``orchestrator`` and the worker guard stays
``ORCHESTRATOR_WORKER``: both are internal, neither is ever seen by a user, and
renaming them would break every installed adapter for no user-visible gain.

Everything a user *can* see is ModelQuad, and is named here rather than spelled
out at each use site, so there is exactly one place to change and exactly one
place to read when asking "what do we call ourselves on this surface".

``LEGACY_MCP_SERVER_TOML_KEY`` is the one pre-rename identifier still read: a
Codex worker's own ``config.toml`` may register the server under that key, and
the worker launch excludes it as well as the current one.
"""

from __future__ import annotations

PRODUCT_NAME = "ModelQuad"
PRODUCT_SLUG = "modelquad"

# Distribution name on PyPI and in importlib.metadata.version() lookups.
DISTRIBUTION_NAME = "modelquad"

# The name each host config registers this MCP server under. Codex's TOML
# table name cannot contain hyphens, hence the separate identifier form.
MCP_SERVER_NAME = "modelquad"
MCP_SERVER_TOML_KEY = "modelquad"
LEGACY_MCP_SERVER_TOML_KEY = "multi_agent_orchestrator"

# Prefix for MCP tool names: modelquad_route, modelquad_delegate, ...
TOOL_PREFIX = "modelquad"

# Console entry points.
CLI_COMMAND = "modelquad"
MCP_COMMAND = "modelquad-mcp"

# Marker tag delimiting our managed region inside a host's own config file
# (CLAUDE.md). Changing this without migrating would orphan the previously
# written block instead of replacing it.
MANAGED_BLOCK_TAG = "modelquad"


def qualified_tool_name(tool: str, *, server: str = MCP_SERVER_NAME) -> str:
    """Return the ``mcp__<server>__<tool>`` name a host reports for one of our tools.

    Hosts address MCP tools by this composed form, so anything matching on tool
    identity has to compose it the same way rather than hard-coding a string
    that silently stops matching the moment either half is renamed.
    """

    return f"mcp__{server}__{tool}"


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
