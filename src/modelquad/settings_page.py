# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``modelquad settings open``: a one-time settings page in the user's browser.

The page changes the same settings the ``modelquad settings`` commands change
(who decides to consult Codex, this project's own Codex setting, and the plans
the user says they are on), in the same ``preferences.json``.

The server lives only while the page is in use:

* It listens on ``127.0.0.1`` on a port the system picks, never on another
  address, and it makes no outgoing connection.
* The page's address carries a random secret made for this run. Every request
  must carry it, the page in its path and each change in a header, and must
  name this server exactly in ``Host`` (so a rebound DNS name is refused) and,
  when the browser sends one, in ``Origin``. A change must be JSON, which a
  form on another site cannot send without the browser asking this server
  first, and this server never agrees.
* Each connection carries one request (HTTP/1.0), so a refused request's body
  is never read as a second request that skips these checks, and a slow
  connection is dropped after :data:`_HANDLER_TIMEOUT_SECONDS`.
* It ends when the user presses Done, or after :data:`IDLE_SECONDS` without a
  request. Nothing keeps running in the background.

When no browser opens, the command prints the page's address, so whoever
reads that output (Claude, when Claude ran the command) can use the page.
That gives nothing the ``modelquad settings`` commands do not already give.

It starts no Codex work, and nothing on the page can: quota and plans are
never inputs to starting work.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable

HOST = "127.0.0.1"
#: The page closes itself after this long without a request.
IDLE_SECONDS = 15 * 60
_MAX_BODY_BYTES = 4096
#: A refused request's body up to this size is read and dropped (see _discard_body).
_DISCARD_LIMIT = 64 * 1024
_HANDLER_TIMEOUT_SECONDS = 15
_TOKEN_HEADER = "X-ModelQuad-Token"
_PAGE_FILE = "settings_page.html"

_GLOBAL_TEXT = {
    True: "Claude may decide on its own to consult Codex.",
    False: "Claude offers Codex when it would help and calls it when you agree or ask.",
}


# ------------------------------------------------------------------ settings


def _repo_view(project: Path | None, proactive: bool) -> dict | None:
    """This project's own word and what is in effect there, or None without a project."""
    if project is None:
        return None
    from . import repo_policy, repo_settings

    mode = repo_settings.read_mode(project)
    result = repo_settings.effective(global_proactive=proactive, user_mode=mode,
                                     policy=repo_policy.load(project))
    file_refuses = result.refusal is not None and result.refusal != repo_settings.USER_CODEX_OFF
    if file_refuses:
        effect = f"Codex is refused here by this repository's {repo_policy.FILE_NAME}."
    elif result.refusal is not None:
        effect = "No Codex in this project: ModelQuad refuses every Codex call here."
    else:
        effect = {
            repo_settings.PROACTIVE: "Claude may decide on its own to consult Codex here.",
            repo_settings.OFFER: "Claude offers Codex here and calls it when you agree or ask.",
            repo_settings.ON_REQUEST: ("Claude calls Codex here only when you ask "
                                       f"(this repository's {repo_policy.FILE_NAME})."),
        }[result.offer]
    return {"folder": str(project), "name": project.name or str(project),
            "mode": mode or "inherit", "in_effect": effect, "file_refuses": file_refuses}


def _plans_view(usage_directory: Path) -> dict:
    from . import plans

    views = plans.current(usage_directory)
    shown = {}
    for provider in plans.PROVIDERS:
        view = views[provider]
        shown[provider] = {
            "declared": view.declared,
            "observed": plans._shown(view.observed) if view.observed else None,
            "counts": view.source,
            "kind": view.kind,
            "choices": list(plans.DECLARABLE_PLANS[provider]),
        }
    return shown


def current_state(project: Path | None, usage_directory: Path) -> dict:
    """Everything the page shows. Read fresh for every request."""
    from .adapters.server_instructions import read_proactive

    proactive = read_proactive()
    return {
        "proactive": proactive,
        "proactive_text": _GLOBAL_TEXT[proactive],
        "project": _repo_view(project, proactive),
        "plans": _plans_view(usage_directory),
    }


class ChangeRefused(ValueError):
    """A change the page sent that this server does not accept. Nothing was written."""


def apply_change(change: object, project: Path | None) -> None:
    """Write one setting, exactly as the matching ``modelquad settings`` command would."""
    from . import plans, repo_settings
    from .adapters.server_instructions import write_proactive

    if not isinstance(change, dict) or set(change) != {"setting", "value"}:
        raise ChangeRefused("Send one setting and its value.")
    setting, value = change["setting"], change["value"]
    if not isinstance(setting, str) or not isinstance(value, str):
        raise ChangeRefused("Send one setting and its value.")
    if setting == "proactive":
        if value not in ("on", "off"):
            raise ChangeRefused("Use on or off.")
        write_proactive(value == "on")
    elif setting == "repo":
        if project is None:
            raise ChangeRefused("This page was opened without a project folder.")
        if value not in repo_settings.CHOICES:
            raise ChangeRefused(f"Use one of: {', '.join(repo_settings.CHOICES)}.")
        repo_settings.write_mode(project, value)
    elif setting in ("plan_claude", "plan_codex"):
        provider = setting.removeprefix("plan_")
        if value != "unset" and value not in plans.DECLARABLE_PLANS[provider]:
            raise ChangeRefused("Pick one of the listed plans.")
        plans.write_declared(provider, None if value == "unset" else value)
    else:
        raise ChangeRefused("ModelQuad does not know that setting.")


# -------------------------------------------------------------------- server


def _page_html(nonce: str) -> bytes:
    text = resources.files("modelquad").joinpath(_PAGE_FILE).read_text(encoding="utf-8")
    return text.replace("{{NONCE}}", nonce).encode("utf-8")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Never share the port with another listener (Windows would otherwise let a
    # second program bind it too).
    allow_reuse_address = False

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks the host name up; this server has no
        # use for a name, so it binds and records the address only.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def handle_error(self, request, client_address) -> None:
        # A browser dropping a connection is not worth a traceback in the
        # command's output; the page shows its own errors.
        return


class SettingsPage:
    """One run of the settings page: its server, its secret and its lifetime."""

    def __init__(self, project: Path | None, usage_directory: Path, *,
                 idle_seconds: float = IDLE_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.project = project
        self.usage_directory = usage_directory
        self.token = secrets.token_urlsafe(32)
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._last_request = clock()
        self._done = threading.Event()
        self.closing = False
        self._write_lock = threading.Lock()
        self.server = _Server((HOST, 0), _handler_for(self))
        self.port = self.server.server_address[1]
        self.origin = f"http://{HOST}:{self.port}"
        self.url = f"{self.origin}/s/{self.token}"

    # The handler calls these.
    def touch(self) -> None:
        self._last_request = self._clock()

    def finish(self) -> None:
        self._done.set()

    def token_matches(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(
            candidate.encode("utf-8", "replace"), self.token.encode("ascii"))

    def serve(self) -> str:
        """Serve until Done or idle; returns ``"done"`` or ``"idle"``."""
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                  name="modelquad-settings-page", daemon=True)
        thread.start()
        try:
            while not self._done.wait(0.5):
                if self._clock() - self._last_request > self.idle_seconds:
                    return "idle"
            return "done"
        finally:
            # Before the listener stops: a connection still open answers nothing more.
            self.closing = True
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)


def _handler_for(page: SettingsPage) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ModelQuad"
        sys_version = ""
        # One request per connection: see the module docstring.
        protocol_version = "HTTP/1.0"
        timeout = _HANDLER_TIMEOUT_SECONDS

        def log_message(self, format, *args):  # noqa: A002 -- the base class's name
            # The path holds the page's secret: nothing is logged.
            return

        # ---------------------------------------------------------- replies

        def _send(self, status: int, body: bytes, content_type: str, *,
                  csp: str = "default-src 'none'; frame-ancestors 'none'") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _refuse(self, status: int = 404) -> None:
            self._discard_body()
            self._send(status, b"Not found.\n", "text/plain; charset=utf-8")

        def _discard_body(self) -> None:
            """Read and drop a refused request's small body before replying.

            Windows resets a connection closed with unread data, and the client
            then loses the reply. The bytes are never parsed: with one request
            per connection (HTTP/1.0) they cannot become a second request.
            """
            if (self.command != "POST" or getattr(self, "_body_read", False)
                    or self.headers.get("Transfer-Encoding") is not None):
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return
            if 0 < length <= _DISCARD_LIMIT:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass

        # ---------------------------------------------------------- checks

        def _from_this_server(self) -> bool:
            """The request names this server exactly and comes from this computer."""
            if page.closing or self.client_address[0] != HOST:
                return False
            if self.headers.get("Host") != f"{HOST}:{page.port}":
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin != page.origin:
                return False
            site = self.headers.get("Sec-Fetch-Site")
            return site in (None, "same-origin", "none")

        # ---------------------------------------------------------- methods

        def do_GET(self) -> None:  # noqa: N802 -- the base class's name
            if not self._from_this_server():
                return self._refuse()
            prefix = "/s/"
            if not self.path.startswith(prefix) or not page.token_matches(self.path[len(prefix):]):
                return self._refuse()
            page.touch()
            nonce = secrets.token_urlsafe(16)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                   "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                   "frame-ancestors 'none'")
            self._send(200, _page_html(nonce), "text/html; charset=utf-8", csp=csp)

        def do_POST(self) -> None:  # noqa: N802 -- the base class's name
            if not self._from_this_server() or not page.token_matches(self.headers.get(_TOKEN_HEADER)):
                return self._refuse()
            if self.headers.get("Transfer-Encoding") is not None:
                return self._refuse(411)
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return self._refuse(415)
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return self._refuse(400)
            if length < 0 or length > _MAX_BODY_BYTES:
                return self._refuse(413)
            body = self.rfile.read(length)
            self._body_read = True
            page.touch()
            if self.path == "/api/state":
                return self._json(200, current_state(page.project, page.usage_directory))
            if self.path == "/api/done":
                self._json(200, {"closed": True})
                page.finish()
                return None
            if self.path != "/api/set":
                return self._refuse()
            try:
                change = json.loads(body.decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                return self._json(400, {"error": "The change could not be read."})
            with page._write_lock:
                if page.closing:
                    return self._refuse()
                try:
                    apply_change(change, page.project)
                except ChangeRefused as exc:
                    return self._json(400, {"error": str(exc)})
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except OSError as exc:
                    return self._json(500, {"error": "The setting was not saved: "
                                            + str(getattr(exc, "strerror", None) or exc)})
            return self._json(200, current_state(page.project, page.usage_directory))

        def do_OPTIONS(self) -> None:  # noqa: N802 -- a cross-site preflight is never agreed to
            self._refuse(405)

    return Handler


def project_folder(project: str | None) -> Path | None:
    """The project the page shows, or None when the folder is not a local project folder."""
    from .localpath import reason as local_reason

    folder = Path(project) if project else Path.cwd()
    if local_reason(os.path.abspath(folder)) is not None or not folder.is_dir():
        return None
    return folder.resolve()
