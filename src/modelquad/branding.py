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

# The name each host config registers this MCP server under. Codex's TOML
# table name cannot contain hyphens, hence the separate identifier form.
MCP_SERVER_NAME = "modelquad"
MCP_SERVER_TOML_KEY = "modelquad"

# Prefix for MCP tool names: modelquad_status, modelquad_delegate, ...
TOOL_PREFIX = "modelquad"

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
