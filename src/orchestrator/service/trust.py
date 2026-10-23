# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The loopback service's trust boundary: which Host and which Origin it serves.

Binding to ``127.0.0.1`` decides who can *reach* the socket. It does not
decide who a browser is acting for when it reaches it:

* **DNS rebinding.** A page on ``attacker.example`` whose name is later
  resolved to ``127.0.0.1`` reaches this service with
  ``Host: attacker.example:8765``, and because the browser considers that its
  own origin it can read every response and call every route. The defence is
  to serve only requests whose ``Host`` names this machine's loopback, and to
  refuse everything else before any route runs.
* **Cross-site requests.** A hostile page can issue a "simple" ``POST``/``PUT``
  to ``http://127.0.0.1:8765/api/...`` with no preflight. It cannot read the
  answer, but the side effect happens. The defence is to accept a
  state-changing request that carries ``Origin`` only when that origin IS
  this request's own authority -- the same host and the same port the
  request's ``Host`` names, which is what "same-origin" means; a page served
  by some other local process on another loopback port is not -- and to refuse
  ``Sec-Fetch-Site: cross-site`` outright. A request with no ``Origin`` at
  all comes from a non-browser client (the CLI, ``doctor``, ``curl``) --
  browsers always attach ``Origin`` to a
  cross-site unsafe request, so its absence is not a bypass.

Both checks are deterministic and fail closed, and they apply to every
method. There is no CORS relaxation for browser origins.

Neither check can tell one local *process* from another, and loopback is
reachable by every account on the computer. So a third check follows them,
and it is default-deny: every request must carry the per-installation secret
in ``X-ModelQuad-Token`` (``service/auth.py``), except ``POST
/api/listener-proof``, the protocol step that proves the listener before a
client sends the secret. The browser checks still run first, so the token
never admits a cross-site page. There is no mode without it: an app built
with no secret refuses every request except the proof.
"""

from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from .auth import TOKEN_HEADER

#: The names a loopback client uses for this machine. Case-insensitive.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Methods that may change state.
UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

MISDIRECTED_STATUS = 421
FORBIDDEN_STATUS = 403


def is_job_path(path: str) -> bool:
    """Exact collection root and slash descendants; never /api/jobs-other."""
    return path == '/api/jobs' or path.startswith('/api/jobs/')

#: The stable, machine-readable ``code`` of each token refusal. A client acts
#: on these, never on the prose ``detail`` beside them.
TOKEN_MISSING = "service_token_missing"
TOKEN_INVALID = "service_token_invalid"
TOKEN_UNAVAILABLE = "service_token_unavailable"
TOKEN_CODES: frozenset[str] = frozenset({TOKEN_MISSING, TOKEN_INVALID, TOKEN_UNAVAILABLE})

_NO_SECRET_REASON = (
    "This service was started without its installation secret, so it refuses "
    "every request. Restart the ModelQuad service."
)


def hostname_of(host_header: str | None) -> str | None:
    """The hostname in a ``Host`` header value, lower-cased, without the port.

    ``[::1]:8765`` -> ``::1``; ``127.0.0.1:8765`` -> ``127.0.0.1``;
    ``localhost`` -> ``localhost``. None for a missing or malformed value --
    which the caller refuses, never tolerates.
    """

    if not isinstance(host_header, str):
        return None
    value = host_header.strip()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end <= 1:
            return None
        rest = value[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None  # "[::1]junk" is not a Host
        return value[1:end].lower()
    name, _, port = value.partition(":")
    name = name.strip().lower()
    if port and not port.isdigit():
        return None
    return name or None


def origin_host(origin: str | None) -> str | None:
    """The hostname of an ``Origin`` header value, or None when it has none."""

    authority = origin_authority(origin)
    return authority[0] if authority else None


def origin_authority(origin: str | None) -> tuple[str, int] | None:
    """``(hostname, port)`` of an ``Origin`` header value, or None.

    The scheme's default port stands in for an absent one, so
    ``http://127.0.0.1`` is ``("127.0.0.1", 80)``.
    """

    if not isinstance(origin, str) or not origin.strip():
        return None
    try:
        parsed = urlsplit(origin.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.hostname.lower(), port)


def host_authority(host_header: str | None) -> tuple[str, int] | None:
    """``(hostname, port)`` of a ``Host`` header value, or None; port 80 when absent."""

    name = hostname_of(host_header)
    if name is None:
        return None
    value = host_header.strip()
    port_text = ""
    if value.startswith("["):
        rest = value[value.find("]") + 1 :]
        port_text = rest[1:] if rest.startswith(":") else ""
    else:
        _, sep, port_text = value.partition(":")
        if not sep:
            port_text = ""
    if not port_text:
        return (name, 80)
    if not port_text.isdigit():
        return None
    return (name, int(port_text))


def requires_token(method: str, path: str) -> bool:
    """Whether a request must carry the installation secret."""

    # Default deny: only the exact proof route is open; Host/Origin still checked.
    return not (method.upper() == "POST" and path == "/api/listener-proof")


def is_fate_route(path: str) -> bool:
    return path.startswith('/api/jobs/') and path.endswith(('/fate', '/release-fate'))


class LoopbackTrustMiddleware:
    """Refuse a request whose ``Host`` is not this service's, a
    state-changing request whose ``Origin`` is not this service's, and a
    guarded request without the installation secret.

    Pure ASGI, wrapping the bound FastAPI.__call__ in LoopbackFastAPI, so
    neither framework dispatch nor its middleware stack sees a refused HTTP
    request. Non-HTTP scopes pass through unchanged.
    """

    def __init__(
        self,
        app: Any,
        *,
        write_secret: str | None = None,
        secret_problem: str | None = None,
    ) -> None:
        self.app = app
        self._secret = write_secret.encode("utf-8") if write_secret else None
        self._secret_problem = secret_problem or _NO_SECRET_REASON

    def refusal(self, headers: Headers, method: str, path: str = "/") -> tuple[int, ...] | None:
        """The status and reason this request is refused for, or None to serve it.

        A token refusal carries a third element: its stable code.
        """

        refused = self._browser_refusal(headers, method, path)
        if refused is not None:
            return refused
        if not requires_token(method, path):
            return None
        refused = self._token_refusal(headers)
        if refused is not None and is_fate_route(path):
            # Secret-verification diagnostics can contain a secret-file path
            # or content. Fate management replies expose only the closed code.
            return (refused[0], refused[2], refused[2])
        return refused

    def _token_refusal(self, headers: Headers) -> tuple[int, str, str] | None:
        if self._secret is None:
            return (FORBIDDEN_STATUS, self._secret_problem, TOKEN_UNAVAILABLE)
        provided = headers.get(TOKEN_HEADER)
        if not provided:
            return (
                FORBIDDEN_STATUS,
                f"Changes need this installation's secret in the {TOKEN_HEADER} header. "
                "Use the modelquad command to change settings.",
                TOKEN_MISSING,
            )
        if not hmac.compare_digest(provided.strip().encode("utf-8"), self._secret):
            return (
                FORBIDDEN_STATUS,
                f"The {TOKEN_HEADER} header does not match this installation's secret. "
                "Use the modelquad command to change settings.",
                TOKEN_INVALID,
            )
        return None

    def _browser_refusal(self, headers: Headers, method: str, path: str = "/") -> tuple[int, str] | None:

        host = hostname_of(headers.get("host"))
        if host is None or host not in LOOPBACK_HOSTS:
            return (
                MISDIRECTED_STATUS,
                "This service answers only to its own machine's loopback names; "
                "the request's Host header names something else.",
            )
        origin = headers.get("origin")
        origin_value = origin.strip() if isinstance(origin, str) else None
        if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
            return (
                FORBIDDEN_STATUS,
                "A cross-site page may not change this service's state.",
            )
        if origin_value is None:
            # A non-browser client; browsers always send Origin here. That
            # does not make it trusted: the token check decides.
            return None
        # Same-origin proper: the page's authority is this request's own --
        # the host AND the port the Host header names. A loopback hostname
        # alone is not enough: another local process's page on another port
        # is another origin.
        if origin_authority(origin_value) != host_authority(headers.get("host")):
            return (
                FORBIDDEN_STATUS,
                "Only this service's own pages may change its state; the request's "
                "Origin is not this service.",
            )
        return None

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        refused = self.refusal(Headers(scope=scope), str(scope.get("method", "GET")), str(scope.get("path", "/")))
        if refused is None:
            if requires_token(str(scope.get('method', 'GET')), str(scope.get('path', '/'))):
                scope['modelquad_authenticated'] = True
            if is_job_path(str(scope.get('path', '/'))):
                async def private_send(message):
                    if message['type'] == 'http.response.start':
                        message = {**message, 'headers': [
                            (key, value) for key, value in message.get('headers', [])
                            if key.lower() != b'cache-control'] + [(b'cache-control', b'no-store')]}
                    await send(message)
                await self.app(scope, receive, private_send)
            else:
                await self.app(scope, receive, send)
            return
        body = {"detail": refused[1]}
        if len(refused) == 3:
            body["code"] = refused[2]
        response = JSONResponse(body, status_code=refused[0],
                                headers={'Cache-Control': 'no-store'} if is_job_path(str(scope.get('path', '/'))) else None)
        await response(scope, receive, send)
