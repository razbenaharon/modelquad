# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The sole credential-bearing service transport. No proxies or reconnects.

Ownership precedes the local secret read; proof and credentials share one
socket. Each new socket gets its own nonce, ownership handle and proof.
Compatibility classification is shared with desktop diagnostics, not treated
as authentication. Errors contain only closed labels, never hostile prose.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import math
from pathlib import Path
import secrets
import socket
import threading
import time

from . import auth
from .listener_proof import RESPONSE_CAP, canonical, derive_key, validate_proof
from .socket_identity import (
    ListenerError, admitted_service_images, endpoint_of, loopback_port_listening, verify_socket_owner,
)

DEFAULT_RESPONSE_CAP = 4 * 1024 * 1024


class ProofTimeout(ListenerError):
    """A proof budget elapsed; distinguish unavailable from invalid identity."""

    def __init__(self):
        super().__init__("service_proof_invalid")


def _expire_socket(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _BoundHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        # HTTPConnection.request/send normally reconnect when sock is None.
        # Here a closed proof connection can NEVER become a credential socket.
        raise ListenerError("service_connection_changed")


@dataclass(frozen=True)
class Reply:
    status: int
    content_type: str
    body: bytes


def _remaining(deadline: float | None, cap: float) -> float:
    if deadline is None:
        return cap
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ListenerError("service_connection_changed")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ListenerError("service_connection_changed")
    return min(cap, remaining)


def compatible_health(health: dict, db_path: str | Path | None = None, *,
                      require_runner: bool = True) -> None:
    from .control_plane import PROBE_READY, classify_control_plane_payload
    from . import db
    from .api import state_identity_tag

    verdict = classify_control_plane_payload(health)
    if verdict.state != PROBE_READY:
        raise ListenerError(verdict.reason or "service_identity_mismatch")
    if type(health.get("db_schema_version")) is not int or health["db_schema_version"] != db.SCHEMA_VERSION:
        raise ListenerError("schema")
    if db_path is not None:
        if health.get("db_identity") != state_identity_tag(Path(db_path).resolve()):
            raise ListenerError("service_identity_mismatch")
        detached = health.get("detached_runner")
        if type(detached) is not dict or type(detached.get("ok")) is not bool:
            raise ListenerError("service_identity_mismatch")
        if require_runner and not detached["ok"]:
            raise ListenerError("service_contained")


class AuthenticatedListener:
    """Owns one socket and one retained owner handle; repr contains no secrets."""

    def __init__(self, connection, sock, owner, secret: str, health: dict, boot: str, *,
                 deadline: float | None = None, require_runner: bool = True):
        self._connection, self._socket, self._owner = connection, sock, owner
        self._secret = secret
        self.health, self.boot = health, boot
        self._endpoint = endpoint_of(sock)
        self._lock = threading.RLock()
        self._deadline, self._require_runner = deadline, require_runner

    def __repr__(self) -> str:
        return "<AuthenticatedListener>"

    def _recheck(self) -> None:
        if self._connection.sock is not self._socket or not self._secret:
            raise ListenerError("service_connection_changed")
        if endpoint_of(self._socket) != self._endpoint:
            raise ListenerError("service_connection_changed")
        self._owner.recheck(self._socket)

    def request(self, method: str, path: str, body: bytes = b"", *, cap: int = DEFAULT_RESPONSE_CAP,
                content_type: str = "application/json", deadline: float | None = None,
                timeout: float = 5.0) -> Reply:
        # Authentication and reads share the caller's budget, even if a caller
        # omits it on the read. A later deadline can never extend that budget.
        if self._deadline is not None:
            deadline = self._deadline if deadline is None else min(deadline, self._deadline)
        sent = False
        timer = None
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ListenerError("service_connection_changed")
        if deadline is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=_remaining(deadline, 5.0)):
            raise ListenerError("service_connection_changed")
        try:
            try:
                if not path.startswith("/api/") or any(c in path for c in ("\r", "\n", "#", "\\")):
                    raise ListenerError("service_identity_mismatch")
                # Runner-independent authentication grants retention reads
                # only; it cannot be used to evade launch admission.
                if not self._require_runner and (method not in {"GET", "HEAD"}
                        or path.split("?", 1)[0] != "/api/pending-output" or body):
                    raise ListenerError("service_contained")
                _remaining(deadline, 5.0)
                self._recheck()
                budget = _remaining(deadline, timeout)
                self._socket.settimeout(budget)
                timer = threading.Timer(budget, _expire_socket, args=(self._socket,))
                timer.daemon = True
                timer.start()
                # From here failure is ambiguous for state-changing callers.
                sent = True
                self._connection.request(method, path, body=body, headers={
                    "Content-Type": content_type, auth.TOKEN_HEADER: self._secret,
                })
                response = self._connection.getresponse()
                if 300 <= response.status < 400:
                    raise ListenerError("service_redirect_refused", sent=True)
                raw = response.read(cap + 1)
                _remaining(deadline, 5.0)
                if len(raw) > cap:
                    raise ListenerError("response_too_large", sent=True)
                return Reply(response.status, response.getheader("Content-Type", "application/json"), raw)
            except Exception as exc:
                self.close()
                code = exc.code if isinstance(exc, ListenerError) else "service_connection_changed"
                raise ListenerError(code, sent=sent) from None
            finally:
                if timer is not None:
                    timer.cancel()
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._secret = ""
            self.health = {}
            self._connection.close()
            self._owner.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def authenticate(port: int, *, db_path: str | Path | None = None,
                 deadline: float | None = None, require_runner: bool = True,
                 expected_pid: int | None = None, expected_created: float | None = None,
                 expected_image: str | None = None) -> AuthenticatedListener:
    sock = owner = connection = None
    timer = None
    expired = threading.Event()
    def expire_proof():
        expired.set()
        _expire_socket(sock)
    try:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ListenerError("service_absent")
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=_remaining(deadline, 0.75))
        except ConnectionRefusedError:
            raise ListenerError("service_absent") from None
        except OSError:
            # No connection was established, so ownership was never observed.
            # In particular a reserved, non-listening Windows port can time
            # out instead of returning connection-refused. Keep that transport
            # failure separate from failures of the ownership check below.
            # Windows delays the reset for a closed loopback port past a short
            # timeout; when the OS table shows no listener, nothing is there.
            if loopback_port_listening(port) is False:
                raise ListenerError("service_absent") from None
            raise ListenerError("service_unreachable") from None
        timer = threading.Timer(_remaining(deadline, 3.0), expire_proof, args=())
        timer.daemon = True
        timer.start()
        images = admitted_service_images(port)
        owner = verify_socket_owner(sock, images)
        if any(value is not None for value in (expected_pid, expected_created, expected_image)):
            owner.verify_expected_process(pid=expected_pid, created=expected_created, image=expected_image)
        _remaining(deadline, 3.0)
        # First local read of any credential comes AFTER Windows ownership.
        from ..paths import state_root
        secret = (auth.read_service_secret(state_root()) if deadline is None else
                  auth.read_service_secret(state_root(), retry_empty=False))
        if not secret:
            raise ListenerError("service_secret_unavailable")
        key = derive_key(secret)
        owner.recheck(sock)
        budget = _remaining(deadline, 0.75)
        sock.settimeout(budget)
        endpoint = endpoint_of(sock)
        connection = _BoundHTTPConnection("127.0.0.1", port, timeout=budget)
        connection.sock = sock
        nonce = secrets.token_hex(32)
        connection.request("POST", "/api/listener-proof", body=canonical({"v": 1, "nonce": nonce}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ListenerError("service_redirect_refused")
        if response.status in (404, 405, 501):
            raise ListenerError("service_auth_unsupported")
        if response.status == 503:
            raise ListenerError("service_secret_unavailable")
        if response.status != 200:
            raise ListenerError("service_proof_invalid")
        raw = response.read(RESPONSE_CAP + 1)
        _remaining(deadline, 3.0)
        health, boot = validate_proof(raw, key, nonce, endpoint)
        compatible_health(health, db_path, require_runner=require_runner)
        if connection.sock is not sock:
            raise ListenerError("service_connection_changed")
        owner.recheck(sock)
        _remaining(deadline, 3.0)
        if any(value is not None for value in (expected_pid, expected_created, expected_image)):
            owner.verify_expected_process(pid=expected_pid, created=expected_created, image=expected_image)
        return AuthenticatedListener(connection, sock, owner, secret, health, boot,
                                     deadline=deadline, require_runner=require_runner)
    except Exception as exc:
        if connection is not None:
            connection.close()
        if sock is not None:
            sock.close()
        if owner is not None:
            owner.close()
        if expired.is_set() or isinstance(exc, (TimeoutError, socket.timeout)):
            raise ProofTimeout() from None
        if isinstance(exc, ListenerError):
            raise
        raise ListenerError("service_owner_unavailable" if owner is None else "service_proof_invalid") from None
    finally:
        if timer is not None:
            timer.cancel()
