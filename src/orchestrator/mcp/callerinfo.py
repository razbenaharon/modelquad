# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Invocation-mode and host provenance for MCP-originated activity records.

Two different trust levels, kept deliberately separate:

`host` is read from the MCP protocol's own `clientInfo` (supplied by the
connecting client's own MCP implementation during `initialize`) -- it is not
something a calling model/agent can fabricate through tool arguments, so it
is treated as protocol-level fact, not a claim.

`clientInfo` carries more than a name, and for some hosts the rest of it is
the only session-scoped evidence there is: Codex spawns MCP servers with a
scrubbed environment but announces `{name: "codex-mcp-client", title:
"Codex", version: "0.147.0"}`. `host_protocol_from_context` exposes the whole
record for `hostobs`; `host_from_context` keeps returning just the raw name,
because the host-to-provider hints match on strings like "claude desktop"
that a normalizer would reject.

A `clientInfo` describes the CONNECTION. `host_request_meta_from_context`
exposes the vendor `_meta` of the single REQUEST being served, which is a
narrower scope. This module does not interpret that payload -- it hands the
raw mapping to `hostobs.host_request_metadata_from_meta`, which is the single
allow-listed boundary that decides what survives. Keeping the extraction here and the policy there is deliberate:
there must be exactly one place that decides what a `_meta` may contribute,
and adding a second reader has to be a visible change rather than a
convenient one.

`invocation_mode` is caller-reported. This server has no way to objectively
prove whether an assistant consulted the orchestrator automatically or
because the user explicitly asked for it, so it never claims to. When a
caller omits it (or passes something unrecognized), the server records
"unknown" with a server_inferred provenance rather than guessing at
automatic vs. explicit -- that is an honest, objective fact about what
information was actually provided, not a guess about intent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover -- import cycle avoidance
    from ..hostobs import HostProtocolIdentity, HostRequestMetadata

VALID_INVOCATION_MODES: frozenset[str] = frozenset(
    {"explicit_user_request", "automatic", "direct_manual", "unknown"}
)


def _client_info(ctx: Any) -> Any:
    """The raw `clientInfo` record, or None outside a real request.

    Fail-safe: a missing session, missing params or missing record all read
    as "the client did not say", never as an error.
    """
    try:
        session = ctx.session
    except (ValueError, AttributeError):
        return None
    try:
        params = session.client_params
    except AttributeError:
        return None
    if params is None:
        return None
    return getattr(params, "client_info", None)


def host_from_context(ctx: Any) -> str | None:
    """The connecting client's self-identified name from MCP's `clientInfo`.

    Returned RAW and unnormalized on purpose: ``provider_for_host`` and
    ``runtime_for_host`` match substrings like ``"claude desktop"``, which a
    token normalizer would reject outright and turn into an unmapped host.
    """
    client_info = _client_info(ctx)
    if client_info is None:
        return None
    return getattr(client_info, "name", None)


def host_protocol_from_context(ctx: Any) -> "HostProtocolIdentity | None":
    """The whole `clientInfo` record, normalized for host observation.

    Returns ``None`` when the client said nothing at all, so a caller can
    distinguish "no MCP connection" from "a connection that identified
    itself with nothing usable". Never raises.
    """
    from ..hostobs import HostProtocolIdentity

    client_info = _client_info(ctx)
    if client_info is None:
        return None
    identity = HostProtocolIdentity(
        name=getattr(client_info, "name", None),
        title=getattr(client_info, "title", None),
        version=getattr(client_info, "version", None),
    )
    return identity if identity else None


def host_request_meta_from_context(ctx: Any) -> "HostRequestMetadata | None":
    """The vendor `_meta` of the request being served, normalized for hostobs.

    Returns None when there is no active request or the request carried no
    `_meta` -- which is the common case, including every `initialize` and both
    legacy tool aliases that take no `ctx`. Callers pass that None straight
    through, and host observation records the channel as consulted-and-empty.

    Fail-safe in the same way `_client_info` is: a missing request context,
    a missing `meta`, or anything unexpected in it reads as "the request said
    nothing", never as an error. Host observability is evidence, and evidence
    must never be able to fail a routing call.

    The mapping is handed on and not retained here. `_meta` is protocol-level
    (the MCP client's own envelope, not a tool argument a model can write), so
    it is trustworthy as to ORIGIN -- but it is still a host payload, and
    `hostobs` is where the allow-list lives.
    """
    from ..hostobs import host_request_metadata_from_meta

    try:
        request_context = ctx.request_context
    except (ValueError, AttributeError):
        return None
    raw = getattr(request_context, "meta", None)
    if raw is None:
        return None
    # `RequestParamsMeta` is a TypedDict, so this is a plain mapping at
    # runtime. It is passed through unconverted on purpose:
    # `host_request_metadata_from_meta` already returns an empty record for
    # anything that is not a dict, and coercing here would introduce the one
    # thing this function promises never to do -- a raise. (`dict(raw)` on a
    # malformed payload does exactly that.)
    record = host_request_metadata_from_meta(raw)
    return record or None


def caller_host_id(
    protocol: "HostProtocolIdentity | None" = None,
    request_meta: "HostRequestMetadata | None" = None,
    client_name: str | None = None,
) -> str | None:
    """Resolve launch identity without reading model settings or session files.

    Reuse host observation's claim boundaries: request, connection, then
    environment. Claude Code is the supported Host, including in the desktop
    app's Code tab; a Codex session is still recognized as a caller.
    """
    from ..hostobs import CodexHostObserver, ClaudeCodeHostObserver

    if CodexHostObserver(protocol=protocol).claims():
        return "codex"
    name = client_name.lower() if isinstance(client_name, str) else (
        protocol.name.lower() if protocol and protocol.name else ""
    )
    if name and not (protocol and protocol.name) and name != "claude-code":
        return None
    if name == "claude-code" or ClaudeCodeHostObserver(protocol=protocol).claims():
        return "claude"
    return None


def resolve_invocation_mode(invocation_mode: str | None) -> tuple[str, str]:
    """Return ``(invocation_mode, invocation_mode_source)``.

    A recognized caller-supplied value is trusted and tagged ``"caller"``.
    An omitted or unrecognized value becomes ``"unknown"`` tagged
    ``"server_inferred"``.
    """
    if invocation_mode in VALID_INVOCATION_MODES:
        return invocation_mode, "caller"
    return "unknown", "server_inferred"
