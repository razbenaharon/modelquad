# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Ask Codex for a genuinely fresh rate-limit observation, with no model turn.

Codex ships an ``app-server``: the same JSON-RPC service its own desktop
client drives, which exposes ``account/rateLimits/read``. Calling it makes
Codex fetch the account's current limits from its backend and hand them back.
Verified live on 2026-08-20: the call answered in 0.8-1.3s, created no session
or conversation file, and returned ``usedPercent: 96`` while the newest
session file on disk still said 94% from two days earlier -- a genuinely newer
observation, obtained without sending a prompt or spending any tokens.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

from .base import UsageSnapshot, no_window_kwargs


CODEX_APPSERVER_SOURCE = "codex_app_server"

APPSERVER_TIMEOUT = 45

NO_WINDOWS = "codex app-server returned no rate-limit window"

# Codex expresses each limit window as a duration in minutes.
_FIVE_HOUR_MINUTES = 300
_SEVEN_DAY_MINUTES = 10080

_SLOT_FOR_MINUTES = {
    _FIVE_HOUR_MINUTES: "five_hour",
    _SEVEN_DAY_MINUTES: "seven_day",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _window_snapshot(window: Any) -> dict[str, Any] | None:
    """Normalize one ``{usedPercent, windowDurationMins, resetsAt}`` window."""

    if not isinstance(window, Mapping):
        return None
    used = _number(window.get("usedPercent"))
    minutes = window.get("windowDurationMins")
    return {
        "used_percentage": used,
        "remaining_percentage": None if used is None else round(100.0 - used, 2),
        "window_minutes": minutes if isinstance(minutes, int) else None,
        "resets_at": window.get("resetsAt"),
    }


def parse_rate_limits_result(
    result: Mapping[str, Any] | Any,
    *,
    now: Callable[[], datetime] | None = None,
) -> UsageSnapshot:
    """Normalize an ``account/rateLimits/read`` result into a usage snapshot.

    Emits the usage-snapshot key set every consumer reads.
    """

    rate_limits = result.get("rateLimits") if isinstance(result, Mapping) else None
    if not isinstance(rate_limits, Mapping):
        rate_limits = {}

    slots: dict[str, dict[str, Any]] = {}
    windows: list[dict[str, Any]] = []
    for key in ("primary", "secondary"):
        snapshot = _window_snapshot(rate_limits.get(key))
        if snapshot is None:
            continue
        # A window is only filed under a named slot when its duration really
        # is that window. Reusing one window for both slots when only a weekly
        # window exists would report a weekly figure as if it were the
        # five-hour one.
        slot = _SLOT_FOR_MINUTES.get(snapshot["window_minutes"], "other")
        if slot != "other" and slot not in slots:
            slots[slot] = snapshot
        windows.append({**snapshot, "slot": slot})

    if not any(window["used_percentage"] is not None for window in windows):
        # An answer with no usable window is not a reading. Reporting it as an
        # undated, unobserved snapshot keeps the last good reading in place
        # (an undated snapshot never erases stored quota).
        return _unobserved(reason=NO_WINDOWS, available=True, now=now)

    five_hour = slots.get("five_hour", {})
    seven_day = slots.get("seven_day", {})

    # A live read means the provider produced this observation right now, so
    # observed_at and collected_at legitimately coincide.
    stamp = (now or (lambda: datetime.now(timezone.utc)))().isoformat()
    return {
        "provider": "codex",
        "available": True,
        "source": CODEX_APPSERVER_SOURCE,
        "refresh_kind": "active",
        "five_hour_used_percentage": five_hour.get("used_percentage"),
        "five_hour_remaining_percentage": five_hour.get("remaining_percentage"),
        "five_hour_resets_at": five_hour.get("resets_at"),
        "seven_day_used_percentage": seven_day.get("used_percentage"),
        "seven_day_remaining_percentage": seven_day.get("remaining_percentage"),
        "seven_day_resets_at": seven_day.get("resets_at"),
        "plan_type": rate_limits.get("planType"),
        "windows": windows,
        "observed_at": stamp,
        "collected_at": stamp,
        "quota_error": None,
    }


def _unobserved(
    *,
    reason: str,
    available: bool,
    now: Callable[[], datetime] | None,
) -> UsageSnapshot:
    """Report that no reading was obtained, without inventing one.

    Availability and quota observability are independent: failing to see the
    quota is never on its own evidence that Codex cannot execute. Only a
    missing CLI marks it unavailable.
    """

    return {
        "provider": "codex",
        "available": available,
        "source": CODEX_APPSERVER_SOURCE,
        "refresh_kind": "unavailable",
        "five_hour_used_percentage": None,
        "five_hour_remaining_percentage": None,
        "five_hour_resets_at": None,
        "seven_day_used_percentage": None,
        "seven_day_remaining_percentage": None,
        "seven_day_resets_at": None,
        "plan_type": None,
        "windows": [],
        "observed_at": None,
        "collected_at": (now or (lambda: datetime.now(timezone.utc)))().isoformat(),
        "quota_error": reason,
        "reason": reason,
    }


def client_info() -> dict[str, str]:
    """How ModelQuad names itself to the app-server: product and real version."""
    from .. import __version__
    from ..branding import installed_version

    return {"name": "modelquad", "version": installed_version(__version__) or __version__}


def _child_environment() -> dict[str, str]:
    """The worker's environment (unrelated secrets removed), with quiet logging.

    ``RUST_LOG=error`` keeps the app-server's log output small; stderr is not
    read at all.
    """
    from ..execution.codex import worker_environment
    from ..executable_resolution import safe_child_env

    env = worker_environment()
    env["RUST_LOG"] = "error"
    return safe_child_env(env)


def _read_result(
    proc: subprocess.Popen,
    *,
    timeout: float,
) -> Mapping[str, Any]:
    """Drive the handshake and return the ``account/rateLimits/read`` result."""

    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def send(message: Mapping[str, Any]) -> None:
        proc.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": client_info(),
                "capabilities": {"experimentalApi": True},
            },
        }
    )

    deadline = time.monotonic() + timeout
    initialized = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("codex app-server did not answer in time")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("codex app-server did not answer in time") from None
        if line is None:
            raise ValueError("codex app-server closed its output stream")
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(message, Mapping):
            continue
        if message.get("id") == 1 and not initialized:
            if message.get("error") is not None or not isinstance(message.get("result"), Mapping):
                raise ValueError("codex app-server refused initialize")
            initialized = True
            send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
            continue
        if message.get("id") == 2:
            if message.get("error") is not None:
                raise ValueError(f"codex app-server returned an error: {message['error']}")
            result = message.get("result")
            if not isinstance(result, Mapping):
                raise ValueError("codex app-server returned no result")
            return result
        # Anything else is an unrelated notification; keep waiting.


def probe_which(name: str) -> str | None:
    # app-server receives fixed arguments and protocol stdin, never a prompt.
    # A batch launcher is admitted only as a fallback when no native codex.exe exists.
    from .. import executable_resolution
    from ..codexcli import project_roots

    # Never a Codex planted under the project folder (codexcli.project_roots):
    # this runs without a prompt, on every quota refresh.
    roots = project_roots()
    if any(char in name for char in ("/", "\\", ":", "\x00")) or not name:
        return executable_resolution.admit_executable(name, allow_batch=True, exclude_roots=roots).path
    return executable_resolution.safe_which(name, allow_batch=True, exclude_roots=roots)


def collect_codex_appserver_usage(
    *,
    binary: str = "codex",
    which: Callable[[str], str | None] | None = None,
    popen: Callable[..., subprocess.Popen] | None = None,
    timeout: float = APPSERVER_TIMEOUT,
    now: Callable[[], datetime] | None = None,
) -> UsageSnapshot:
    """Read Codex's current account rate limits through its own app-server.

    The native executable is preferred over an npm launcher. The child runs
    in a kill-on-close Job Object (``procutil.start_contained``) and its whole
    tree is stopped afterwards, so a launcher's cmd -> node -> codex chain
    never outlives the read. ``popen`` replaces the contained start in tests.
    """

    from .. import procutil
    from ..codexcli import which_codex

    # An explicit candidate can be refused; never publish its path in a snapshot.
    lookup_label = "codex" if any(char in binary for char in ("/", "\\", ":", "\x00")) else binary
    lookup = which if which is not None else probe_which
    resolved = which_codex(binary, which=lookup, native=True) or which_codex(binary, which=lookup)
    if resolved is None:
        return _unobserved(
            reason=f"codex CLI not found on PATH (looked for {lookup_label!r}).",
            available=False,
            now=now,
        )

    launch = dict(
        stdout=subprocess.PIPE,
        # Never read: an undrained pipe could fill and stall the child.
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        env=_child_environment(),
        # The one caller that legitimately overrides the detached-stdin
        # default: this speaks a request/response protocol to the Codex
        # app-server over the child's stdin, so it needs a real pipe. Every
        # other product-spawned child gets DEVNULL, which is what stops a
        # Windows helper from prompting on an inherited console.
        stdin=subprocess.PIPE,
        **no_window_kwargs(detach_stdin=False),
    )
    job = None
    try:
        if popen is None:
            proc, job = procutil.start_contained([resolved, "app-server"], **launch)
        else:
            proc = popen([resolved, "app-server"], **launch)
    except FileNotFoundError:
        return _unobserved(
            reason=f"codex CLI not found on PATH (looked for {lookup_label!r}).",
            available=False,
            now=now,
        )
    except OSError as exc:
        return _unobserved(
            reason=f"Failed to launch codex app-server: {exc}",
            available=True,
            now=now,
        )

    try:
        result = _read_result(proc, timeout=timeout)
    except TimeoutError as exc:
        return _unobserved(reason=str(exc), available=True, now=now)
    except (OSError, ValueError) as exc:
        return _unobserved(
            reason=f"codex app-server rate-limit read failed: {exc}",
            available=True,
            now=now,
        )
    finally:
        # The child is a long-running service: it never exits on its own, so
        # it must always be torn down, on every path, with its whole tree.
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        if popen is None:
            procutil.stop_contained(proc, job)
        else:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    return parse_rate_limits_result(result, now=now)
