# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""ModelQuad's own outbound network rule, enforced at run time.

ModelQuad's own Python process connects only to loopback addresses.
:func:`install_loopback_guard` makes that a runtime check rather than a
promise: it adds an audit hook (``sys.addaudithook``) that refuses
``socket.connect`` and ``socket.sendto`` to any address that is not loopback,
raising :class:`OutboundConnectionRefused` before the call is made. The
entry points install it: the ``modelquad`` command and ``python -m modelquad``
(``cli_entry.main``), and the MCP server (``mcp.server.main``).

Only calls that raise those audit events are seen, which means calls made
through Python's ``socket`` module. A connect by Windows asyncio's Proactor
event loop (``_overlapped.ConnectEx``) and a host name lookup
(``socket.getaddrinfo``) raise neither and are not refused. ModelQuad's own
code makes neither; NETWORK.md says so to users.

The guard covers ModelQuad's own process only. The Codex CLI that ModelQuad
starts is a separate program; it connects to OpenAI itself, under the user's
own login, and this hook does not see or restrict it.
"""

from __future__ import annotations

import sys

LOOPBACK_ONLY_REASON = "ModelQuad's own process connects only to loopback addresses"

_GUARDED_EVENTS = frozenset({"socket.connect", "socket.sendto"})
_guard_installed = False
#: Whether the installed hook refuses. Tests that run an entry point in-process
#: switch it back off; nothing in the product does.
guard_active = False


class OutboundConnectionRefused(PermissionError):
    """A connection from ModelQuad's own process to a non-loopback address."""


def is_loopback_address(address: object) -> bool:
    """Whether a socket address names this machine.

    A path (an ``AF_UNIX`` address) is local by definition. An ``(host,
    port, ...)`` tuple is loopback when ``host`` is ``localhost`` or a
    loopback IP literal, including an IPv4-mapped IPv6 one. Any other host
    name is refused: resolving it would itself be a lookup this rule does
    not vouch for.
    """

    if isinstance(address, (str, bytes)):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    host = host.strip().strip("[]").lower().split("%", 1)[0]
    if host == "localhost":
        return True
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or bool(mapped is not None and mapped.is_loopback)


def _audit(event: str, args: tuple) -> None:
    if not guard_active or event not in _GUARDED_EVENTS:
        return
    address = args[1] if len(args) > 1 else None
    if not is_loopback_address(address):
        raise OutboundConnectionRefused(f"{LOOPBACK_ONLY_REASON}; refused {event}")


def install_loopback_guard() -> None:
    """Refuse non-loopback connections from this process from now on.

    Idempotent. An audit hook cannot be removed once added, so it is added
    once per process and :data:`guard_active` turns its refusal on.
    """

    global _guard_installed, guard_active
    if not _guard_installed:
        sys.addaudithook(_audit)
        _guard_installed = True
    guard_active = True
