# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Listener proof v1: bounded, strict JSON and a secret-derived HMAC.

No transcript is retained or logged. Endpoint facts come only from the actual
accepted ASGI transport; production Uvicorn disables proxy header rewriting.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets

from .socket_identity import ListenerError

REQUEST_CAP = 1024
RESPONSE_CAP = 65536
_BOOT = secrets.token_hex(16)
_HEX32 = re.compile(r"[0-9a-f]{64}")
_HEX16 = re.compile(r"[0-9a-f]{32}")
_DOMAIN = b"ModelQuad/listener-proof/v1\0"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def parse_json(raw: bytes, *, cap: int) -> object:
    """Bound depth before Python's recursive decoder; refuse duplicate keys."""
    try:
        if len(raw) > cap:
            raise ValueError
        text = raw.decode("utf-8", errors="strict")
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 16:
                    raise ValueError
            elif char in "]}":
                depth -= 1
                if depth < 0:
                    raise ValueError

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        def constant(_value):
            raise ValueError

        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        # Reject e.g. 1e999, which json.loads accepts as infinity.
        canonical(value)
        return value
    except Exception:
        raise ListenerError("service_proof_invalid") from None


def derive_key(secret: str) -> bytes:
    try:
        if type(secret) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise ValueError
        return hmac.digest(secret.encode("ascii"), b"ModelQuad/listener-proof/v1/key", "sha256")
    except (ValueError, UnicodeError):
        raise ListenerError("service_secret_unavailable") from None


def valid_endpoint(endpoint: object) -> bool:
    return (type(endpoint) is list and len(endpoint) == 4
            and endpoint[0] == endpoint[2] == "127.0.0.1"
            and all(type(endpoint[i]) is int and 1 <= endpoint[i] <= 65535 for i in (1, 3)))


def request_nonce(value: object) -> str:
    if (type(value) is not dict or set(value) != {"v", "nonce"}
            or type(value["v"]) is not int or value["v"] != 1
            or type(value["nonce"]) is not str or not _HEX32.fullmatch(value["nonce"])):
        raise ListenerError("service_proof_invalid")
    return value["nonce"]


def make_proof(key: bytes, nonce: str, endpoint: list, health: dict, *, boot: str = _BOOT) -> dict:
    if not valid_endpoint(endpoint):
        raise ListenerError("service_auth_unsupported")
    result = {"v": 1, "nonce": nonce, "boot": boot, "endpoint": endpoint, "health": health}
    result["mac"] = hmac.new(key, _DOMAIN + canonical(result), hashlib.sha256).hexdigest()
    if len(canonical(result)) > RESPONSE_CAP:
        raise ListenerError("service_proof_invalid")
    return result


def validate_proof(raw: bytes, key: bytes, nonce: str, endpoint: list) -> tuple[dict, str]:
    value = parse_json(raw, cap=RESPONSE_CAP)
    if (type(value) is not dict or set(value) != {"v", "nonce", "boot", "endpoint", "health", "mac"}
            or type(value["v"]) is not int or value["v"] != 1
            or value["nonce"] != nonce or type(value["nonce"]) is not str
            or not valid_endpoint(value["endpoint"]) or value["endpoint"] != endpoint
            or type(value["boot"]) is not str or not _HEX16.fullmatch(value["boot"])
            or type(value["health"]) is not dict
            or type(value["mac"]) is not str or not _HEX32.fullmatch(value["mac"])):
        raise ListenerError("service_proof_invalid")
    supplied = value.pop("mac")
    expected = hmac.new(key, _DOMAIN + canonical(value), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise ListenerError("service_proof_invalid")
    return value["health"], value["boot"]


async def proof_response(request):
    from fastapi.responses import JSONResponse
    from .api import get_health

    headers = {"Cache-Control": "no-store"}
    try:
        key = request.app.state.listener_proof_key
        if key is None:
            raise ListenerError("service_secret_unavailable")

        async def read():
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > REQUEST_CAP:
                    raise ListenerError("service_proof_invalid")
                raw.extend(chunk)
            return bytes(raw)

        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdecimal() or int(declared) > REQUEST_CAP):
            raise ListenerError("service_proof_invalid")
        nonce = request_nonce(parse_json(await asyncio.wait_for(read(), timeout=2), cap=REQUEST_CAP))
        client, server = request.scope.get("client"), request.scope.get("server")
        if not client or not server or len(client) != 2 or len(server) != 2:
            raise ListenerError("service_auth_unsupported")
        endpoint = [client[0], client[1], server[0], server[1]]
        return JSONResponse(make_proof(key, nonce, endpoint, get_health(request)), headers=headers)
    except ListenerError as exc:
        status = 503 if exc.code in {"service_secret_unavailable", "service_auth_unsupported"} else 400
        return JSONResponse(exc.body(), status_code=status, headers=headers)
    except Exception:
        return JSONResponse(ListenerError("service_proof_invalid").body(), status_code=400, headers=headers)
